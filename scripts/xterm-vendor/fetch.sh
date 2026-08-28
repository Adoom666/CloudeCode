#!/usr/bin/env bash
# Re-download the pinned xterm.js assets vendored under client/vendor/xterm/
# and verify each one against the sha256 recorded in this script.
#
# Unlike scripts/codemirror-vendor/ (an npm build workspace that bundles
# CodeMirror's ES module packages into one file), xterm.js ships these five
# files as plain pre-built browser assets straight from its npm packages -
# there is nothing to bundle, so this is a plain curl script instead of an
# npm build.
#
# Usage:
#   scripts/xterm-vendor/fetch.sh
#
# To bump a version: edit the version variables and the matching sha256 in
# EXPECTED_SHA256 below, run this script, and update
# client/vendor/xterm/VERSION.md with whatever it prints.
#
# Exits non-zero and prints which file failed if a download is empty or its
# sha256 does not match what is pinned here.

set -euo pipefail

XTERM_VERSION="5.3.0"
FIT_VERSION="0.8.0"
WEBGL_VERSION="0.16.0"
UNICODE11_VERSION="0.6.0"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="$SCRIPT_DIR/../../client/vendor/xterm"

# file -> source URL
declare -A SOURCE_URL=(
    [xterm.css]="https://cdn.jsdelivr.net/npm/xterm@${XTERM_VERSION}/css/xterm.css"
    [xterm.js]="https://cdn.jsdelivr.net/npm/xterm@${XTERM_VERSION}/lib/xterm.js"
    [xterm-addon-fit.js]="https://cdn.jsdelivr.net/npm/xterm-addon-fit@${FIT_VERSION}/lib/xterm-addon-fit.js"
    [xterm-addon-webgl.js]="https://cdn.jsdelivr.net/npm/xterm-addon-webgl@${WEBGL_VERSION}/lib/xterm-addon-webgl.js"
    [xterm-addon-unicode11.js]="https://cdn.jsdelivr.net/npm/xterm-addon-unicode11@${UNICODE11_VERSION}/lib/xterm-addon-unicode11.js"
)

# file -> expected sha256. Update these when you bump a pinned version.
declare -A EXPECTED_SHA256=(
    [xterm.css]="832f3f2c603b43ad4351ff04970150cc7a873014276db126a6065c6dd81e4872"
    [xterm.js]="f0aea0f75f48559013ae6643c2479dd737d26da42d5524e6d2b70915ae6523c7"
    [xterm-addon-fit.js]="10f3194c5f17c1786fb7d5db865c1ec8539b6736a318063fd38bdaaf7c46848f"
    [xterm-addon-webgl.js]="0c9c48c9391c4cee816eacf95699dbde97e8cc8f191e87f3a571e73d214c8df8"
    [xterm-addon-unicode11.js]="ab10d83642883e5e17ea741cd5b6e5f8c0f6a06e3271f2f0c0e043be4fc5e738"
)

sha256_of() {
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d' ' -f1
    else
        sha256sum "$1" | cut -d' ' -f1
    fi
}

mkdir -p "$OUT_DIR"

echo "fetching pinned xterm assets into $OUT_DIR"

for file in "${!SOURCE_URL[@]}"; do
    url="${SOURCE_URL[$file]}"
    dest="$OUT_DIR/$file"

    echo "  -> $file  ($url)"
    curl -sSfL -o "$dest" "$url"

    size=$(wc -c <"$dest" | tr -d ' ')
    if [ "$size" -eq 0 ]; then
        echo "FAIL: $file downloaded empty from $url" >&2
        exit 1
    fi

    actual_sha256=$(sha256_of "$dest")
    expected_sha256="${EXPECTED_SHA256[$file]}"

    if [ "$actual_sha256" != "$expected_sha256" ]; then
        echo "FAIL: sha256 mismatch for $file" >&2
        echo "  expected: $expected_sha256" >&2
        echo "  actual:   $actual_sha256" >&2
        echo "  if this is a deliberate version bump, update EXPECTED_SHA256" >&2
        echo "  in this script and client/vendor/xterm/VERSION.md together" >&2
        exit 1
    fi

    echo "     ok  ($size bytes, sha256 $actual_sha256)"
done

echo "all five xterm assets verified against pinned sha256"
