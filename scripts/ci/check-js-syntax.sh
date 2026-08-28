#!/usr/bin/env bash
#
# check-js-syntax.sh - parse every browser JavaScript file with node --check.
#
# Description:
#   The client is served as plain static files with no bundler, transpiler or
#   test runner, so a syntax error in client/js reaches the browser unnoticed
#   and breaks the app at load time. This script is the cheapest possible
#   guard: node --check parses each file and reports syntax errors without
#   executing anything.
#
# Inputs:
#   $1 (string, optional) - single directory to scan. When omitted, every
#                           directory in DEFAULT_SCAN_DIRS below is scanned,
#                           resolved relative to the repo root.
#
#   macOS/ is in that list deliberately. It was not until 2026-08-21, so the
#   Electron main process - which spawns the uninstaller - was the one JS
#   tree in this repo that CI never parsed. A syntax error there breaks the
#   tray app at launch exactly the way one in client/js breaks the browser.
#
#   The client root replaced client/js on 2026-08-26 for the same reason.
#   Scanning client/js meant that client/setup.js - served to the browser
#   like everything else - and all 24 theme scripts under
#   client/css/themes/ had never been parsed by CI at all. They are shipped
#   JS in a repo with no bundler; a syntax error in one is a broken page.
#   client/vendor is pruned: it is third-party bundled code this project
#   does not author or edit, and parsing it only adds noise.
#
# MODULE GOAL, AND WHY EVERY FILE IS TRIED BOTH WAYS
#   node --check assumes CommonJS for a bare .js extension and has no flag
#   to say otherwise for a named file, so it throws a SyntaxError on valid
#   ES module source. Every theme effects.js in this repo is an ES module,
#   so a naive widening of the scan root would have reported 24 syntax
#   errors in 24 correct files. Each file is therefore parsed as CommonJS
#   first and, only if that fails, re-parsed as a module by feeding it to
#   `node --input-type=module --check` on stdin. A file is reported broken
#   only when BOTH goals reject it, which is the honest question - is this
#   text parseable as JavaScript at all - and cannot false-fail on a
#   correct file whichever goal it was written for.
#
# Outputs:
#   Exit code 0 - every file parsed cleanly.
#   Exit code 1 - at least one file failed to parse; each failure is printed
#                 to stderr with the node diagnostic.
#   Exit code 2 - the target directory does not exist or holds no .js files.
#
# Example:
#   scripts/ci/check-js-syntax.sh
#   scripts/ci/check-js-syntax.sh client/js
#
set -uo pipefail

DEFAULT_SCAN_DIRS=("client" "macOS")

# node_modules and vendor paths are pruned inside the find below. Both are
# third-party bundled code this project does not author or edit.

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

targets=()
if [ "$#" -gt 0 ]; then
    targets+=("$1")
else
    for d in "${DEFAULT_SCAN_DIRS[@]}"; do
        targets+=("${repo_root}/${d}")
    done
fi

for target in "${targets[@]}"; do
    if [ ! -d "${target}" ]; then
        echo "check-js-syntax: not a directory: ${target}" >&2
        exit 2
    fi
done

checked=0
failed=0

# Description: parse one file, trying CommonJS then ES module.
# Inputs:  $1 (string) - absolute path to a .js file.
# Outputs: exit 0 when either goal accepts it; exit 1 when both reject it,
#          with the CommonJS diagnostic printed (it is the more familiar
#          one, and a file that fails both is broken in both).
# Example: check_one /repo/client/js/app.js
check_one() {
    local file="$1" cjs_err
    if cjs_err="$(node --check "${file}" 2>&1)"; then
        return 0
    fi
    if node --input-type=module --check < "${file}" >/dev/null 2>&1; then
        modules=$((modules + 1))
        return 0
    fi
    printf '%s\n' "${cjs_err}" >&2
    return 1
}

modules=0

for target in "${targets[@]}"; do
    while IFS= read -r -d '' file; do
        checked=$((checked + 1))
        if ! check_one "${file}"; then
            echo "check-js-syntax: SYNTAX ERROR in ${file}" >&2
            failed=$((failed + 1))
        fi
    done < <(
        find "${target}" -type f -name '*.js' \
            -not -path '*/node_modules/*' \
            -not -path '*/vendor/*' -print0 | sort -z
    )
done

if [ "${checked}" -eq 0 ]; then
    # An empty result almost always means a moved directory or a bad path
    # argument, not a genuinely empty client. Fail loudly rather than
    # reporting a vacuous pass.
    echo "check-js-syntax: no .js files found under ${targets[*]}" >&2
    exit 2
fi

if [ "${failed}" -gt 0 ]; then
    echo "check-js-syntax: ${failed} of ${checked} file(s) failed to parse" >&2
    exit 1
fi

echo "check-js-syntax: ${checked} file(s) parsed cleanly (${modules} as ES modules)"
exit 0
