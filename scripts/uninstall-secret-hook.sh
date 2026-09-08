#!/bin/sh
# Remove the pre-commit secret scan from this clone's .git/hooks.
#
# It only removes a hook carrying OUR marker. A pre-commit hook somebody
# else installed is left exactly where it is and reported, because
# silently deleting another tool's hook is a worse failure than not
# uninstalling. If the installer backed up a previous hook, this restores
# the most recent backup.
#
# Usage: ./scripts/uninstall-secret-hook.sh

set -eu

MARKER="# cloudecode-secret-scan"

REPO_ROOT=$(git rev-parse --show-toplevel)
HOOK_DIR=$(git rev-parse --git-path hooks)
case "$HOOK_DIR" in /*) ;; *) HOOK_DIR="$REPO_ROOT/$HOOK_DIR" ;; esac
TARGET="$HOOK_DIR/pre-commit"

if [ ! -e "$TARGET" ]; then
    echo "nothing to do: $TARGET does not exist"
    exit 0
fi

if ! grep -q "$MARKER" "$TARGET" 2>/dev/null; then
    echo "REFUSING: $TARGET exists but was not installed by us." >&2
    echo "Left untouched. Remove it by hand if that is what you want." >&2
    exit 1
fi

rm -f "$TARGET"
echo "removed: $TARGET"

BACKUP=$(ls -1t "$TARGET".pre-secret-scan.* 2>/dev/null | head -1 || true)
if [ -n "$BACKUP" ]; then
    mv "$BACKUP" "$TARGET"
    chmod +x "$TARGET"
    echo "restored previous hook from $BACKUP"
fi
