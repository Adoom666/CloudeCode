---
description: Upgrade this CloudeCode install and verify the data actually migrated
---

Upgrade this CloudeCode checkout to a newer release and then PROVE the data
survived. Follow `docs/upgrade-with-claude.md` exactly; it is the authority
and it explains why each step exists.

Target tag, if the user named one: $ARGUMENTS
If they named none, upgrade to the newest published release tag.

Work in this order and do not skip step 1:

1. `./scripts/upgrade-baseline.sh`
   Read the output. If it exits 2, STOP and tell the user why: an upgrade
   you cannot verify is one they should not start.

2. `./scripts/upgrade.sh $ARGUMENTS`
   Read the printed backup path and its `.manifest`. Say what was backed up
   and what was legitimately not present.

3. `./scripts/upgrade-verify.sh`
   Exit 0 = every check passed. Exit 1 = something FAILED. Exit 2 = at least
   one check COULD NOT BE EVALUATED and none failed. Two is not zero.

4. Confirm the running server is serving the NEW code by content, not by
   claim: hash an asset fetched over HTTP against the git blob from the new
   tag. `git rev-parse` only reads back the claim the deploy already
   believes; it is not an independent measurement.

Then report the MEASUREMENTS, not a verdict word. Version before and after,
schema version before and after, per-table row counts, the migration trail
entry, the backup path. A schema version that did not move is normal and
expected on most upgrades; what matters is that it did not move BACKWARDS
and that no table lost rows.

If any check could not be evaluated, say which one and why, in those words.
Never report an upgrade as successful on the strength of a check that did not
run. If something failed, `./scripts/rollback.sh` restores the newest backup.
