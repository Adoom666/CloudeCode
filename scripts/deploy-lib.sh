#!/bin/bash
# deploy-lib.sh - transfer and verification helpers for deploy-mini.sh.
#
# WHY THIS IS A SEPARATE FILE. Two reasons. The verification half is
# useful on its own (deploy-mini.sh --verify-only re-checks a target at
# any time, which is exactly what HANDOFF.md asks for after a restart),
# and keeping it here holds both files well under the 500 line rule.
#
# WHY TAR AND NOT RSYNC. Measured 2026-09-07: both this Mac and the mini
# ship Apple openrsync (protocol 29, "rsync 2.6.9 compatible"), not GNU
# rsync 3.x. openrsync handles --files-from=- --relative correctly; what
# it does NOT survive is a remote destination containing SPACES. The
# remote shell word-splits the path and the receiver reports
#
#     server receiver mode requires two argument
#
# Both live destinations contain spaces ("Application Support" and
# "Cloude Code.app"), so `--target live` could never work while
# `--target v11` always did. Escaping the spaces does fix rsync, but it
# leaves a trap that reappears the moment somebody adds a destination.
# tar over ssh has no such edge: the remote command is one quoted string
# this script controls, and the destination is handed over on stdin
# rather than spliced into a command line. It also costs no new
# dependency, which rsync 3.x would.
#
# THE SHAPE OF A DEPLOY. Stage, verify, then commit:
#   1. tar the file list into ONE staging dir on the mini
#   2. sha256 the staging dir against the local files
#   3. only then copy staging into each real destination
#   4. sha256 each destination against the local files
# Step 2 is what makes this fail safe. A broken pipe or a corrupted
# transfer is caught while production is still untouched.
#
# All functions run with the repo root as the working directory and take
# file lists as newline delimited paths relative to it. A path containing
# a newline is not supported and would break tar -T as well.

# dl_local_hashes - sha256 every file in a list, from the repo root.
# Inputs:  $1 listfile (path), $2 outfile (path)
# Output:  writes "<sha256>  <path>" lines to outfile; returns 0, or 1 if
#          the list is empty.
# Example: dl_local_hashes /tmp/files.txt /tmp/local.sha
dl_local_hashes() {
    local listfile="$1" outfile="$2"
    [ -s "$listfile" ] || return 1
    tr '\n' '\0' < "$listfile" | xargs -0 shasum -a 256 > "$outfile" 2>/dev/null
}

# dl_remote_hashes - sha256 the same file list inside a remote directory.
# Inputs:  $1 host, $2 remote dest dir, $3 listfile, $4 outfile
# Output:  writes "<sha256>  <path>" lines, or "MISSING  <path>" for a
#          file the target does not have, or the single token
#          __NO_DEST__ when the directory itself is unreachable.
# Notes:   ONE ssh round trip. The destination travels as the first line
#          of stdin, never on the command line, so a path with spaces
#          cannot be word split. A file that cannot be hashed becomes an
#          explicit MISSING line rather than vanishing from the
#          comparison, because absent on both sides compares equal and
#          that is how a verification step silently checks nothing.
# Example: dl_remote_hashes mac-mini-m4 "/Applications/X.app" list.txt out.sha
dl_remote_hashes() {
    local host="$1" dest="$2" listfile="$3" outfile="$4"
    { printf '%s\n' "$dest"; cat "$listfile"; } | ssh "$host" '
        IFS= read -r DEST
        cd "$DEST" 2>/dev/null || { echo "__NO_DEST__"; exit 0; }
        TMP=$(mktemp -t cloudedeploy) || exit 1
        cat > "$TMP"
        [ -s "$TMP" ] || { rm -f "$TMP"; exit 0; }
        while IFS= read -r f; do
            [ -f "$f" ] || printf "MISSING  %s\n" "$f"
        done < "$TMP"
        tr "\n" "\0" < "$TMP" | xargs -0 shasum -a 256 2>/dev/null
        rm -f "$TMP"
    ' > "$outfile" 2>/dev/null
}

# dl_norm - reduce a hash listing to sorted "<path><TAB><hash>" lines.
# Inputs:  $1 infile, $2 outfile
# Output:  none; writes outfile. Sorting by path makes the comparison
#          order independent instead of resting on shasum emitting the
#          list back in the order it was fed.
dl_norm() {
    sed -e 's/^\([0-9a-f]\{64\}\)  \(.*\)$/\2	\1/' \
        -e 's/^MISSING  \(.*\)$/\1	MISSING/' "$1" \
        | LC_ALL=C sort > "$2"
}

# dl_verify - prove a remote directory holds exactly the local bytes.
# Inputs:  $1 host, $2 remote dest, $3 listfile, $4 local hash file,
#          $5 human label for messages
# Output:  returns 0 on a byte for byte match, 4 on any mismatch or
#          missing file, 3 when the check could not be evaluated at all.
#          Prints the offending paths on failure.
# Notes:   This compares CONTENT on both sides. `git rev-parse` on either
#          end is not verification, it only reads back the claim the
#          deploy already believes.
dl_verify() {
    local host="$1" dest="$2" listfile="$3" localhash="$4" label="$5"
    local rh nl nr
    rh=$(mktemp -t cloudeverify) || return 3
    nl=$(mktemp -t cloudeverify) || return 3
    nr=$(mktemp -t cloudeverify) || return 3

    dl_remote_hashes "$host" "$dest" "$listfile" "$rh"

    if [ ! -s "$rh" ]; then
        echo "  CANNOT DETERMINE: no hash output came back from $label." >&2
        rm -f "$rh" "$nl" "$nr"; return 3
    fi
    if grep -q '__NO_DEST__' "$rh"; then
        echo "  CANNOT DETERMINE: $label does not exist or is unreadable:" >&2
        echo "    $dest" >&2
        rm -f "$rh" "$nl" "$nr"; return 3
    fi

    dl_norm "$localhash" "$nl"
    dl_norm "$rh" "$nr"

    if cmp -s "$nl" "$nr"; then
        rm -f "$rh" "$nl" "$nr"; return 0
    fi

    echo "  HASH MISMATCH at $label:" >&2
    echo "    $dest" >&2
    # Report the paths whose hash differs, not the raw diff, so the
    # operator sees which files are wrong rather than a wall of hex.
    LC_ALL=C join -t'	' -j1 "$nl" "$nr" 2>/dev/null \
        | awk -F'\t' '$2 != $3 { printf "    %s  (local %s, target %s)\n", $1, substr($2,1,12), substr($3,1,12) }' >&2
    LC_ALL=C comm -23 <(cut -f1 "$nl") <(cut -f1 "$nr") \
        | sed 's/^/    absent from target: /' >&2
    rm -f "$rh" "$nl" "$nr"
    return 4
}

# dl_stage - tar a file list from here into a fresh remote staging dir.
# Inputs:  $1 host, $2 listfile, $3 remote staging dir (no spaces, this
#          script chooses it)
# Output:  returns 0 on success, 1 if the archive or the extract failed.
# Notes:   The caller must have `set -o pipefail` for a failure in the
#          sending half to be seen.
dl_stage() {
    local host="$1" listfile="$2" stagedir="$3"
    # shellcheck disable=SC2029  # $stagedir is ours and must expand here
    ssh "$host" "rm -rf '$stagedir' && mkdir -p '$stagedir'" || return 1
    # shellcheck disable=SC2029  # $stagedir is ours and must expand here
    tar -cf - -T "$listfile" | ssh "$host" "tar -xf - -C '$stagedir'" || return 1
}

# dl_commit - copy a verified staging dir into a real destination.
# Inputs:  $1 host, $2 remote staging dir, $3 destination dir
# Output:  returns ditto's exit status.
# Notes:   ditto MERGES the tree rather than replacing it - it adds and
#          overwrites, it never removes a file the destination has that the
#          staging dir does not. That is exactly right for adds/updates and
#          exactly wrong for a git-level delete, which is why this call is
#          followed by dl_prune_stale below rather than relied on alone to
#          make a destination match HEAD. ditto creates the destination and
#          any intermediate directories, and is a single command with a
#          real exit status (no remote pipeline whose status would hide the
#          sending half). The destination goes over stdin so spaces in it
#          cannot be word split. Correctness does not rest on this call
#          reporting truthfully: every destination is hashed independently
#          afterwards, and (see below) checked for leftovers ditto would
#          never have removed.
dl_commit() {
    local host="$1" stagedir="$2" dest="$3"
    printf '%s\n' "$dest" | ssh "$host" \
        'IFS= read -r DEST; mkdir -p "$DEST" && ditto '"'$stagedir'"' "$DEST"'
}

# dl_unstage - remove a remote staging dir. Best effort, never fatal.
# Inputs:  $1 host, $2 remote staging dir
# Output:  always returns 0.
dl_unstage() {
    # shellcheck disable=SC2029  # the staging path is ours and must expand here
    ssh "$1" "rm -rf '$2'" >/dev/null 2>&1 || true
    return 0
}

# ---------------------------------------------------------------- mirroring
# MIRROR, NOT MERGE. ditto (dl_commit above) only ever adds and overwrites,
# so a file removed from git stays on a destination forever - Punchlist #23.
# The functions below make a destination match the git-tracked set for
# src/ and client/ exactly: present files hashed equal (dl_verify, above)
# AND no file present that isn't tracked at HEAD (dl_verify_no_extra,
# dl_prune_stale, here). Both directions have to hold for "mirror" to be
# true; checking only the first is the asymmetric blind spot Punchlist #23
# is about - a check that only enumerates files that SHOULD exist can never
# see a file that should not.
#
# SCOPE IS HARD-LIMITED to the src/ and client/ subtrees of a destination.
# The server dir also holds .env and a venv that must survive a deploy;
# the bundle holds Electron's own files. dl_find_tree only ever descends
# into <dest>/src and <dest>/client, dl_reject_unscoped is a second,
# independent check on every path before it reaches rm, and dl_prune_stale
# additionally requires each deleted path to literally start with "src/"
# or "client/". Three independent reasons a bug anywhere in this chain
# cannot turn into a delete outside the two subtrees it is allowed to
# touch.
#
# dl_run_on - run a remote-shaped shell command, over ssh, or (when host is
# the sentinel "__local__") directly via bash against a plain local
# directory standing in for a target. Every function below that touches a
# "host" goes through this, which is what lets tests/test_deploy_mirror.sh
# prove the mirror/prune logic against a throwaway directory without ever
# opening an ssh connection to a real target.
# Inputs:  $1 host ("__local__" or a real ssh destination), $2 command
# Output:  whatever the command prints; stdin passes through unchanged.
dl_run_on() {
    local host="$1" cmd="$2"
    if [ "$host" = "__local__" ]; then
        bash -c "$cmd"
    else
        # shellcheck disable=SC2029  # $cmd is the REMOTE-side script on
        # purpose - callers build it as a single-quoted literal so its own
        # $DEST/$f references expand on the far end, never here.
        ssh "$host" "$cmd"
    fi
}

# dl_find_tree - list every file under <dest>/src and <dest>/client, paths
# relative to dest (e.g. "src/main.py"), sorted.
# Inputs:  $1 host, $2 dest dir, $3 outfile
# Output:  writes sorted relative paths to outfile, excluding __pycache__
#          dirs, *.pyc and .DS_Store (build/OS noise, never deploy state);
#          or the single token __NO_DEST__ when dest is unreadable.
# Example: dl_find_tree mac-mini-m4 "/Applications/X.app/Contents/Resources" out.txt
dl_find_tree() {
    local host="$1" dest="$2" outfile="$3"
    # shellcheck disable=SC2016  # deliberately single-quoted: $DEST below
    # is the REMOTE shell's variable, read from the piped stdin, not this
    # one's.
    printf '%s\n' "$dest" | dl_run_on "$host" '
        IFS= read -r DEST
        cd "$DEST" 2>/dev/null || { echo "__NO_DEST__"; exit 0; }
        for d in src client; do
            [ -d "$d" ] || continue
            find "$d" -type f \
                ! -path "*/__pycache__/*" \
                ! -name "*.pyc" \
                ! -name ".DS_Store"
        done | LC_ALL=C sort
    ' > "$outfile" 2>/dev/null
}

# dl_stale_paths - pure diff: paths present in "found" but not in "keep".
# Inputs:  $1 foundfile (sorted relative paths), $2 keepfile (any order),
#          $3 outfile
# Output:  writes sorted stale paths to outfile (possibly empty). No I/O
#          beyond reading its two inputs - this is what a unit test
#          exercises without touching ssh or a filesystem tree at all.
dl_stale_paths() {
    local foundfile="$1" keepfile="$2" outfile="$3"
    LC_ALL=C comm -23 "$foundfile" <(LC_ALL=C sort "$keepfile") > "$outfile"
}

# dl_reject_unscoped - refuse a path list unless every line starts with
# "src/" or "client/". Independent of where the list came from; this is
# the last gate before a delete, so it is checked even though dl_find_tree
# already only descends into those two subtrees.
# Inputs:  $1 file of relative paths
# Output:  returns 0 if every line is in scope; prints offending lines to
#          stderr and returns 1 otherwise.
dl_reject_unscoped() {
    local file="$1"
    if grep -qvE '^(src|client)/' "$file" 2>/dev/null; then
        echo "  REFUSING: path(s) outside src/ or client/:" >&2
        grep -vE '^(src|client)/' "$file" | sed 's/^/    /' >&2
        return 1
    fi
    return 0
}

# dl_prune_stale - remove files under <dest>/src or <dest>/client that are
# not in the tracked-at-HEAD keep list, i.e. leftovers from a git-level
# delete that dl_commit's ditto merge would never clean up on its own.
# Inputs:  $1 host, $2 dest dir, $3 keepfile (sorted tracked src/client
#          paths, ALL extensions - see all_tracked_files() in
#          deploy-mini.sh, not the EXT_RE-filtered set this script
#          deploys, so a tracked-but-undeployed file such as a vendor
#          asset is never mistaken for a leftover), $4 dry (1 = report
#          only, delete nothing; default 0)
# Output:  returns 0 (nothing stale, or dry run reported it), 1 (delete
#          command failed, or a computed path failed dl_reject_unscoped),
#          3 (cannot determine - dest unreadable). Prints what it removed
#          or would remove.
# Example: dl_prune_stale mac-mini-m4 "$DEST_SERVER" "$ALLTRACKED" 0
dl_prune_stale() {
    local host="$1" dest="$2" keepfile="$3" dry="${4:-0}"
    local found stale n rc

    found=$(mktemp -t cloudeprune) || return 3
    dl_find_tree "$host" "$dest" "$found"

    if [ ! -s "$found" ]; then
        echo "  CANNOT DETERMINE: no listing came back for $dest" >&2
        rm -f "$found"; return 3
    fi
    if grep -q '__NO_DEST__' "$found"; then
        echo "  CANNOT DETERMINE: destination does not exist or is unreadable:" >&2
        echo "    $dest" >&2
        rm -f "$found"; return 3
    fi

    stale=$(mktemp -t cloudeprune) || { rm -f "$found"; return 3; }
    dl_stale_paths "$found" "$keepfile" "$stale"
    rm -f "$found"

    if [ ! -s "$stale" ]; then
        rm -f "$stale"
        return 0
    fi

    n=$(wc -l < "$stale" | tr -d ' ')
    if ! dl_reject_unscoped "$stale"; then
        rm -f "$stale"; return 1
    fi

    if [ "$dry" -eq 1 ]; then
        echo "  would remove $n stale file(s) not tracked at HEAD:"
        sed 's/^/    /' "$stale"
        rm -f "$stale"
        return 0
    fi

    echo "  removing $n stale file(s) not tracked at HEAD:"
    sed 's/^/    /' "$stale"

    # shellcheck disable=SC2016  # deliberately single-quoted: $DEST and $f
    # below are the REMOTE shell's variables, read from the piped stdin.
    { printf '%s\n' "$dest"; cat "$stale"; } | dl_run_on "$host" '
        IFS= read -r DEST
        cd "$DEST" 2>/dev/null || exit 1
        while IFS= read -r f; do
            case "$f" in
                src/*|client/*) rm -f -- "$f" ;;
                *) exit 1 ;;  # belt and suspenders; dl_reject_unscoped already checked
            esac
        done
    '
    rc=$?
    rm -f "$stale"
    return "$rc"
}

# dl_verify_no_extra - the negative check: confirm nothing untracked
# remains under <dest>/src or <dest>/client. This is what closes the
# asymmetric blind spot - dl_verify only ever proves the files that
# should be present ARE, and by itself cannot see a file that should not
# be there at all.
# Inputs:  $1 host, $2 dest dir, $3 keepfile (see dl_prune_stale), $4
#          human label for messages
# Output:  returns 0 (clean), 4 (untracked files remain - same failure
#          family dl_verify uses for a hash mismatch), 3 (cannot
#          determine). Prints the offending paths on failure.
dl_verify_no_extra() {
    local host="$1" dest="$2" keepfile="$3" label="$4"
    local found extra

    found=$(mktemp -t cloudeprune) || return 3
    dl_find_tree "$host" "$dest" "$found"

    if [ ! -s "$found" ]; then
        echo "  CANNOT DETERMINE: no listing came back for $label." >&2
        rm -f "$found"; return 3
    fi
    if grep -q '__NO_DEST__' "$found"; then
        echo "  CANNOT DETERMINE: $label does not exist or is unreadable:" >&2
        echo "    $dest" >&2
        rm -f "$found"; return 3
    fi

    extra=$(mktemp -t cloudeprune) || { rm -f "$found"; return 3; }
    dl_stale_paths "$found" "$keepfile" "$extra"
    rm -f "$found"

    if [ ! -s "$extra" ]; then
        rm -f "$extra"
        return 0
    fi

    echo "  UNTRACKED FILES REMAIN at $label (not tracked at HEAD):" >&2
    sed 's/^/    /' "$extra" >&2
    rm -f "$extra"
    return 4
}

# dl_combine_rc - fold two outcome codes from the {0, 3, 4} family into
# one, worst-wins: a definite failure (4) outranks cannot-determine (3),
# which outranks success (0). Lets a caller run dl_verify and
# dl_verify_no_extra independently and report ONE outcome without ever
# letting a "could not tell" mask a "confirmed wrong", or a stale success
# survive alongside either.
# Inputs:  $1 code a, $2 code b
# Output:  prints the combined code (0, 3, or 4).
dl_combine_rc() {
    local a="$1" b="$2"
    if [ "$a" -eq 4 ] || [ "$b" -eq 4 ]; then echo 4; return; fi
    if [ "$a" -eq 3 ] || [ "$b" -eq 3 ]; then echo 3; return; fi
    echo 0
}
