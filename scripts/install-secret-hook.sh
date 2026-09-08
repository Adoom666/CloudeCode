#!/bin/sh
# Install the pre-commit secret scan into this clone's .git/hooks.
#
# WHY AN INSTALLER EXISTS AT ALL. .git/hooks is not version controlled,
# so a hook committed to the repo does nothing until somebody copies it
# into place. This script is that copy step, and it is the distribution
# mechanism for every clone.
#
# IT IS DELIBERATELY LOUD AND DELIBERATELY REVERSIBLE. It prints the
# exact path it writes, refuses to clobber a pre-commit hook it did not
# write, and names the uninstaller. A hook the owner cannot find or turn
# off is worse than none.
#
# Usage: ./scripts/install-secret-hook.sh [--force]

set -eu

MARKER="# cloudecode-secret-scan"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

REPO_ROOT=$(git rev-parse --show-toplevel)
HOOK_DIR=$(git rev-parse --git-path hooks)
case "$HOOK_DIR" in /*) ;; *) HOOK_DIR="$REPO_ROOT/$HOOK_DIR" ;; esac
TARGET="$HOOK_DIR/pre-commit"
SOURCE="$REPO_ROOT/scripts/hooks/pre-commit-secret-scan.sh"

[ -f "$SOURCE" ] || { echo "missing $SOURCE" >&2; exit 1; }
mkdir -p "$HOOK_DIR"

if [ -e "$TARGET" ] && ! grep -q "$MARKER" "$TARGET" 2>/dev/null; then
    if [ "$FORCE" -eq 0 ]; then
        echo "REFUSING: $TARGET already exists and is not ours." >&2
        echo "Inspect it, then re-run with --force to back it up and replace it." >&2
        exit 1
    fi
    BACKUP="$TARGET.pre-secret-scan.$(date +%Y%m%d%H%M%S)"
    cp "$TARGET" "$BACKUP"
    echo "backed up existing hook to $BACKUP"
fi

{
    echo "#!/bin/sh"
    echo "$MARKER installed by scripts/install-secret-hook.sh"
    echo "$MARKER remove with scripts/uninstall-secret-hook.sh"
    echo "exec \"$SOURCE\" \"\$@\""
} > "$TARGET"
chmod +x "$TARGET"

echo "installed: $TARGET"
echo "  runs:      $SOURCE"
echo "  uninstall: $REPO_ROOT/scripts/uninstall-secret-hook.sh"
echo "  bypass:    git commit --no-verify"
echo "  audit now: ./venv/bin/python3 scripts/scan_secrets.py"
