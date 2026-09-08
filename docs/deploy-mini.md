# Deploying to the mini

This is a **developer tool**, not the end-user upgrade path. If you are
upgrading an existing install, see `docs/upgrade-with-claude.md` instead -
that is a from-source checkout upgrading itself. This document covers
pushing code FROM this repo TO `mac-mini-m4`, which is how development
against a live/packaged install actually happens: you develop here, the
mini runs it, and `scripts/deploy-mini.sh` is the only supported way to
close that loop.

`scripts/deploy-mini.sh` (driver) and `scripts/deploy-lib.sh` (transfer,
verification and mirror helpers) tar `src/` and `client/` over ssh into a
staging dir on the mini, sha256 the staging dir against this Mac BEFORE
anything real is touched, then copy staging into each real destination and
re-hash it there independently. See the script's own header (`-h`) for the
two targets (`live`, port 8000; `v11`, port 8001, disposable) and why the
`live` target has two mandatory destinations - the app bundle's `Resources`
and the menubar app's `server` support dir - written in that order so the
one recoverable partial-failure state is the one you end up in.

## Deploys mirror `src/` and `client/`, they do not merely merge into them

The copy step (`dl_commit` in `deploy-lib.sh`) runs `ditto`, which only
ever adds and overwrites a destination - it never removes a file the
destination has that the staging dir does not. Until this was fixed, that
was the entire deploy mechanism, and it had a real defect: **the first
commit that deleted a file under `src/` or `client/` left the stale copy on
both targets, indefinitely, while the deploy still reported success**,
because verification only ever hashed the files that were supposed to be
present. A check that only enumerates files that SHOULD exist is
structurally unable to notice one that should not.

Every deploy now follows the `ditto` copy with `dl_prune_stale`
(`deploy-lib.sh`), which removes any file under a destination's `src/` or
`client/` that is not tracked in git at HEAD. The keep-list it prunes
against is `all_tracked_files()` in `deploy-mini.sh` - **every** tracked
extension under `src/`/`client/`, not just the `.py`/`.js`/`.css`/`.html`/
`.json` set this script actually copies (`$EXT_RE`), so a tracked-but-
undeployed asset (a doc, a vendor font) is never mistaken for a leftover
and deleted by accident.

Verification then runs in **both directions**:

- `dl_verify` - every expected file is present and byte-correct (existing).
- `dl_verify_no_extra` - nothing untracked remains under `src/` or
  `client/` (new - the negative check this document is about).

`dl_combine_rc` folds the two outcomes worst-wins across the same
three-outcome family the rest of the script already uses: `0` success,
`4` definite failure (reused for either a hash mismatch or a leftover
file - both mean "the target does not match HEAD"), `3` cannot-determine
(an unreadable destination; never silently read as clean). `--verify-only`
runs both checks read-only; `--dry-run` previews what a real run would
prune, also read-only, against the real destinations.

## Why the prune can never reach outside `src/` or `client/`

The server dir also holds `.env` and a venv that must survive a deploy;
the bundle holds Electron's own files. Three independent gates keep the
prune inside its two subtrees:

1. `dl_find_tree` only ever descends into `<dest>/src` and `<dest>/client`
   in the first place - nothing outside them is ever listed as a
   candidate.
2. `dl_reject_unscoped` refuses any computed path that does not literally
   start with `src/` or `client/`, checked again right before deletion.
3. The remote delete loop itself only acts on a `src/*` or `client/*`
   path; anything else falls through to `exit 1` rather than a silent
   no-op.

## Proving it without touching a real target

`dl_run_on`'s `__local__` sentinel routes every remote-shaped call in
`deploy-lib.sh` through plain `bash` against an ordinary local directory
instead of `ssh`. `tests/test_deploy_mirror.sh` uses that to prove the
whole find/prune/verify path - including the three scope gates above and
the three-outcome exit codes - against a throwaway directory standing in
for a deploy target, without ever opening an ssh connection. Run it
directly:

    ./tests/test_deploy_mirror.sh

It is a plain bash test (same shape as `tests/test_resolve_port.sh`) since
there is no pytest bridge for shell functions; it prints PASS/FAIL per
case and exits non-zero if any case fails.

## Restarting the live app: `launchctl kickstart`, not quit-and-reopen

`deploy-mini.sh --target live` does NOT relaunch the Electron app: it kills
the pid owning port 8000 and lets the menubar app respawn the server. That
is enough for a code deploy. When you need to restart the APP itself, use:

    launchctl kickstart -k gui/$(id -u)/com.cloudecode.menubar

Do **not** restart it with `osascript` quit plus `open -a`. That path
starts the app as a fresh GUI launch, which macOS registers under an ad
hoc launchd job named `application.com.cloudecode.menubar.<hash>` instead
of the real `com.cloudecode.menubar` label. The app runs and serves
normally, so nothing looks wrong - but the ad hoc job's stdout goes to
`/dev/null`, so `/tmp/cloudecode-menubar.log` silently stops growing and
the next person to debug a boot problem finds a log that ends hours ago
with no error explaining why. `kickstart -k` stops and restarts the real
job in place, keeping the label and the log.

Verify after any restart:

    launchctl list | grep cloudecode

The real `com.cloudecode.menubar` label must show a pid, and no
`application.com.cloudecode.menubar.*` entry may be present. Confirm
`/tmp/cloudecode-menubar.log` has a current mtime and is growing.

The two logs are different files and answer different questions:

| File | Holds |
|---|---|
| `/tmp/cloudecode-menubar.log` | the Electron menubar wrapper's own stdout |
| `~/Library/Application Support/cloude-code-menubar/logs/server.log` | the Python server, structlog JSON - this is where `boot_readopt_complete` lives |
