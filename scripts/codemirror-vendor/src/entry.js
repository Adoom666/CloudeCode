/**
 * Build-only entry point for the vendored CodeMirror 6 bundle shipped at
 * client/vendor/codemirror/codemirror-bundle.js. This file is NOT shipped
 * itself — esbuild bundles it (see package.json's "build" script) into a
 * single minified IIFE that exposes window.CodeMirrorBundle. Kept
 * deliberately small: only the language modes config-editor-modal.js
 * actually needs (markdown, json, python, javascript, shell/bash via
 * @codemirror/legacy-modes rather than pulling in a full separate
 * @codemirror/lang-* package for a mode that doesn't have one), plus a
 * minimal editing/viewing feature set (line numbers, active-line
 * highlight, bracket matching, history/undo, default keymap, search).
 *
 * Deliberately excluded to keep bundle size down: no theme package (the
 * app supplies its own CSS matching config-editor.css's existing design
 * tokens instead of shipping @codemirror/theme-one-dark), no autocomplete
 * UI wiring (imported only because @codemirror/lang-python depends on it
 * transitively - not exposed/enabled here), no full language-data
 * "kitchen sink" package.
 */
import { EditorState, Compartment } from '@codemirror/state';
import {
    EditorView, keymap, lineNumbers, highlightActiveLine,
    highlightActiveLineGutter, drawSelection,
} from '@codemirror/view';
import {
    defaultKeymap, history, historyKeymap, indentWithTab,
} from '@codemirror/commands';
import {
    indentOnInput, bracketMatching, syntaxHighlighting, defaultHighlightStyle,
    foldGutter, foldKeymap,
} from '@codemirror/language';
import { searchKeymap, highlightSelectionMatches } from '@codemirror/search';
import { markdown } from '@codemirror/lang-markdown';
import { json } from '@codemirror/lang-json';
import { python } from '@codemirror/lang-python';
import { javascript } from '@codemirror/lang-javascript';
import { StreamLanguage } from '@codemirror/language';
import { shell } from '@codemirror/legacy-modes/mode/shell';

/**
 * Description: map a file extension to its CodeMirror language extension.
 *   Falls back to no language support (plain text, still gets line
 *   numbers/selection/etc.) for anything unrecognized - this editor must
 *   never refuse to open a file just because it doesn't know the
 *   language.
 * Inputs: path (string) - file path or name, used only for its extension.
 * Output: Extension|null - a CodeMirror language extension, or null.
 * Example: languageForPath('deploy.sh') -> shell mode extension.
 */
function languageForPath(path) {
    const ext = String(path || '').toLowerCase().split('.').pop();
    switch (ext) {
        case 'md':
        case 'markdown':
            return markdown();
        case 'json':
            return json();
        case 'py':
            return python();
        case 'js':
        case 'cjs':
        case 'mjs':
            return javascript();
        case 'sh':
        case 'bash':
        case 'zsh':
            return StreamLanguage.define(shell);
        default:
            return null;
    }
}

/**
 * Description: build a fresh EditorState for one file's content.
 * Inputs: content (string) - initial document text; path (string) - used
 *   to pick a language mode; readOnly (bool) - true disables editing;
 *   onChange (function(string): void) - called with the new full document
 *   text on every change (used for dirty-tracking).
 * Output: EditorState.
 */
function buildState(content, path, readOnly, onChange) {
    const languageExt = languageForPath(path);
    const extensions = [
        lineNumbers(),
        highlightActiveLineGutter(),
        highlightActiveLine(),
        drawSelection(),
        history(),
        indentOnInput(),
        bracketMatching(),
        foldGutter(),
        highlightSelectionMatches(),
        syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
        keymap.of([
            ...defaultKeymap,
            ...historyKeymap,
            ...searchKeymap,
            ...foldKeymap,
            indentWithTab,
        ]),
        EditorView.lineWrapping,
        EditorState.readOnly.of(!!readOnly),
        EditorView.updateListener.of((update) => {
            if (update.docChanged && typeof onChange === 'function') {
                onChange(update.state.doc.toString());
            }
        }),
    ];
    if (languageExt) extensions.push(languageExt);
    return EditorState.create({ doc: content || '', extensions });
}

/**
 * Description: create and mount a CodeMirror editor into a container
 *   element. This is the ONE entry point client/js/config-editor-modal.js
 *   calls - it never touches CodeMirror's internal modules directly, so
 *   the vendored bundle's internal shape can change without touching the
 *   app's own code.
 * Inputs: container (Element) - mount point, replaces its children;
 *   content (string) - initial text; path (string) - for language
 *   detection; readOnly (bool); onChange (function(string): void).
 * Output: object - { getValue(): string, setContent(string, path,
 *   readOnly): void, setReadOnly(bool): void, focus(): void,
 *   destroy(): void }.
 */
function createEditor(container, content, path, readOnly, onChange) {
    const state = buildState(content, path, readOnly, onChange);
    const view = new EditorView({ state, parent: container });

    return {
        getValue() {
            return view.state.doc.toString();
        },
        setContent(newContent, newPath, newReadOnly) {
            view.setState(buildState(newContent, newPath, newReadOnly, onChange));
        },
        setReadOnly(newReadOnly) {
            view.setState(buildState(view.state.doc.toString(), path, newReadOnly, onChange));
        },
        focus() {
            view.focus();
        },
        destroy() {
            view.destroy();
        },
    };
}

// The bundle's ONLY public surface. This assignment - not esbuild's
// --global-name flag - is what defines the global, deliberately: the two
// are mutually exclusive. --global-name wraps the whole bundle as
// `var CodeMirrorBundle = (() => { ... })()`, and since this entry has no
// `export`, that IIFE returns undefined and the `var` immediately
// overwrites the object set here. Keeping the name in one place, in
// source, also means `window.X` is set explicitly rather than relying on
// a top-level `var` happening to become a window property (which is only
// true for a classic, non-module script).
window.CodeMirrorBundle = { createEditor, languageForPath };
