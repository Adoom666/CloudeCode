#!/bin/bash
# Shared restore-on-exit / restore-on-signal primitives for every
# scripts/ci/mutate-*.sh harness.
#
# WHY THIS EXISTS. All 21 mutation scripts edit a source file, run a
# test suite against the mutation, then restore the file - and each one
# had grown its own copy of the backup/restore/trap plumbing (some via a
# FILES[] array + BAKDIR, some via individual BAK_X mktemp files). None
# of them trapped SIGINT or SIGTERM, only EXIT. A run killed by a tool
# timeout (SIGTERM) or Ctrl-C (SIGINT) could leave the mutated file
# sitting in the working tree, and the NEXT script to run against that
# file reads the mutant as if it were source - see CLAUDE.md hazard on
# the tmux_listing_parse.py false failure this caused.
#
# WHAT WAS ACTUALLY VERIFIED, NOT ASSUMED. A bare `trap ... EXIT` DOES
# fire on SIGTERM delivered straight to the script's own PID (bash does
# not defer SIGTERM while waiting on a foreground command). It also DOES
# fire on SIGINT or SIGTERM delivered to the whole process group (the
# child dies too, which unblocks bash's wait immediately). But POSIX
# shell behaviour REQUIRES bash to *defer* SIGINT delivery - even with a
# trap explicitly registered for it - until the current foreground
# command finishes, when that signal reaches only the shell's own PID
# and not the child it is waiting on. In that one case, `trap ... EXIT`,
# and even `trap ... EXIT INT TERM` with the mutation's test command run
# as a plain foreground command, both hang until the child exits on its
# own - verified empirically with a 30s sleep standing in for a slow
# test run, both with and without an explicit INT trap; the state file
# stayed "mutated" the whole time either way.
#
# The one construction that is actually interrupt-safe under ALL of
# those delivery mechanisms: run the test command in the BACKGROUND,
# `wait` on its PID, and have the signal trap explicitly kill that
# tracked PID before restoring. Killing the child unblocks the `wait`
# immediately regardless of how the signal reached the parent, so the
# trap always gets to run. That is what mutate_run() below does, and it
# is the only thing in this file that changes RUNTIME behaviour - the
# rest is the same backup/restore bookkeeping every script already did,
# just written once.
#
# USAGE (see any scripts/ci/mutate-*.sh for a live example):
#   source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/mutate-trap.sh"
#   mutate_arm_trap "$ROOT" "${FILES[@]}"   # snapshot + install EXIT/INT/TERM trap
#   ...
#   mutate_restore_files                     # explicit restore before each mutation
#   ...
#   mutate_run node "$ROOT/tests/foo.node.mjs"   # in place of a bare `node ...`
#   echo $?                                       # the child's real exit status
#
# Globals this file owns (do not reassign from a sourcing script):
#   MUTATE_ROOT, MUTATE_FILES, MUTATE_BAKDIR, MUTATE_CHILD_PID

MUTATE_ROOT=""
MUTATE_FILES=()
MUTATE_BAKDIR=""
MUTATE_CHILD_PID=""

# Description: resolve one caller-supplied path against MUTATE_ROOT. A
#   path that is already absolute (starts with "/") is returned as-is -
#   a handful of callers keep their FILES[] array as absolute paths
#   already built from $ROOT, and forcing them to relativize it first
#   would be a needless second source of truth for the same path.
# Inputs: f (str) - a path, relative to MUTATE_ROOT or already absolute.
# Output: str (via echo) - the absolute path.
mutate_resolve_path() {
    case "$1" in
        /*) echo "$1" ;;
        *)  echo "${MUTATE_ROOT}/$1" ;;
    esac
}

# Description: snapshot every file that will be mutated and install the
#   EXIT/INT/TERM trap that restores them all, unconditionally.
# Inputs: root (str) - repo root, absolute path.
#   files... (str...) - one or more paths, each either relative to root
#   or already absolute, that a mutate() call in the sourcing script
#   will edit in place.
# Output: none. Sets MUTATE_ROOT/MUTATE_FILES/MUTATE_BAKDIR globals and
#   arms the trap. Exits 1 if a target file does not exist.
mutate_arm_trap() {
    MUTATE_ROOT="$1"; shift
    MUTATE_FILES=("$@")
    MUTATE_BAKDIR="$(mktemp -d)"
    local i=0 f abs
    for f in "${MUTATE_FILES[@]}"; do
        abs="$(mutate_resolve_path "$f")"
        if [ ! -f "$abs" ]; then
            echo "mutate_arm_trap: no such file: $abs" >&2
            exit 1
        fi
        cp "$abs" "${MUTATE_BAKDIR}/${i}"
        i=$((i + 1))
    done
    # Separate handlers, not one `trap mutate_cleanup EXIT INT TERM`. A
    # signal trap that does not itself call exit is, by design, just a
    # handled interruption: bash resumes the script right where it left
    # off once the handler returns. That is fatal here - the NEXT
    # mutate() call in the caller's loop would then call restore() again
    # against a MUTATE_BAKDIR this same handler already deleted, silently
    # do nothing (empty bakdir is treated as "nothing armed"), and apply
    # a fresh mutation on top of a file that was never actually restored.
    # This was measured, not assumed: a live SIGTERM against a real
    # mutate-*.sh left the target file permanently mutated for exactly
    # this reason even though the restore inside the handler itself had
    # already run correctly once. EXIT needs no forced exit (the shell is
    # already on its way out); INT/TERM must force one, which is why they
    # pass mutate_cleanup a real exit code and it acts on it below.
    trap 'mutate_cleanup 0' EXIT
    trap 'mutate_cleanup 130' INT
    trap 'mutate_cleanup 143' TERM
}

# Description: copy every armed file back to its pre-mutation original.
#   Safe to call repeatedly (each mutate() call in the caller restores
#   before applying its own edit) and safe to call with nothing armed.
# Inputs: none. Output: none.
mutate_restore_files() {
    [ -z "$MUTATE_BAKDIR" ] && return 0
    local i=0 f
    for f in "${MUTATE_FILES[@]}"; do
        cp "${MUTATE_BAKDIR}/${i}" "$(mutate_resolve_path "$f")"
        i=$((i + 1))
    done
}

# Description: the EXIT/INT/TERM trap target. Kills whatever test child
#   mutate_run left tracked (unblocking a deferred-SIGINT wait, see the
#   file header), restores every mutated file, then removes the backup
#   directory. Idempotent - a second invocation (EXIT firing after an
#   INT/TERM handler already forced an exit) is a harmless no-op because
#   MUTATE_BAKDIR is cleared at the end.
# Inputs: exit_code (int, optional, default 0) - 0 means "the shell is
#   already exiting on its own, just clean up"; anything else means this
#   fired from a signal trap that would otherwise let the script resume,
#   so this function disarms the traps (no recursive re-entry from the
#   exit below) and forcibly exits with that code once cleanup is done.
# Output: none - either returns (code 0 path) or does not return (calls
#   exit for a nonzero code).
mutate_cleanup() {
    local exit_code="${1:-0}"
    # Kill only - do NOT wait() on it here. This handler can itself run
    # while mutate_run's own `wait "$MUTATE_CHILD_PID"` is still on the
    # call stack (a signal trap interrupts a builtin wait synchronously
    # in bash); a second wait on the same pid from inside that nested
    # context was measured to abort the rest of this function silently,
    # skipping the restore below entirely - kill(1) alone is enough to
    # unblock the outer wait, which reaps the child itself.
    if [ -n "$MUTATE_CHILD_PID" ]; then
        kill "$MUTATE_CHILD_PID" 2>/dev/null
        MUTATE_CHILD_PID=""
    fi
    mutate_restore_files
    [ -n "$MUTATE_BAKDIR" ] && rm -rf "$MUTATE_BAKDIR"
    MUTATE_BAKDIR=""
    if [ "$exit_code" != "0" ]; then
        trap - EXIT INT TERM
        exit "$exit_code"
    fi
}

# Description: run one test command the way every mutate() body needs
#   to - as a backgrounded, tracked child so an INT/TERM that reaches
#   only this script's own PID can still interrupt it immediately (see
#   file header for why a plain foreground call cannot be interrupted
#   that way). Blocks until the child exits, exactly like running it in
#   the foreground would, and returns the same exit status.
# Inputs: "$@" - the command and its arguments (e.g. node, a path, args).
# Output: none directly; sets $? to the child's real exit status, and
#   anything the child wrote to stdout/stderr is inherited normally (the
#   caller still redirects with >/dev/null 2>&1 if it wants quiet).
# Example: mutate_run node "$ROOT/tests/foo.node.mjs"; if [ $? -eq 0 ]...
mutate_run() {
    "$@" &
    MUTATE_CHILD_PID=$!
    wait "$MUTATE_CHILD_PID"
    local status=$?
    MUTATE_CHILD_PID=""
    return "$status"
}
