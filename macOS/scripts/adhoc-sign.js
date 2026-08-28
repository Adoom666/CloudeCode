'use strict';

/**
 * electron-builder `afterPack` hook: ad-hoc sign the packed .app bundle.
 *
 * WHY THIS EXISTS
 * ---------------
 * electron-builder signs only when it can resolve a real signing identity.
 * Locally there is none (`security find-identity -v -p codesigning` reports
 * "0 valid identities found") and CI explicitly sets
 * CSC_IDENTITY_AUTO_DISCOVERY=false, so in BOTH environments it logs
 * "skipped macOS application code signing" and moves on with exit 0.
 *
 * The bundle it leaves behind is not merely unsigned, it is BROKEN. There is
 * no `Contents/_CodeSignature` directory at all, so the only signature is the
 * ad-hoc one the linker puts on the arm64 Mach-O executable. That combination
 * makes both of these fail:
 *
 *     codesign --verify --deep --strict "Cloude Code.app"
 *     spctl --assess --type execute "Cloude Code.app"
 *
 * with "code has no resources but signature indicates they must be present".
 * Once a browser download adds com.apple.quarantine, macOS does not show the
 * ordinary "unidentified developer" prompt (which right click > Open clears).
 * It shows "Cloude Code is damaged and can't be opened", which right click >
 * Open does NOT clear. The user's only escape is `xattr -dr
 * com.apple.quarantine` in Terminal. For a release aimed at non-developers
 * that is a hard blocker.
 *
 * A proper ad-hoc signature (`codesign --sign -`) writes a real
 * `Contents/_CodeSignature/CodeResources` sealing the bundle's resources. The
 * signature is structurally valid but carries no identity, so Gatekeeper gives
 * the ordinary unidentified-developer prompt instead of "damaged". That is the
 * goal: be no worse than v0.8.1, which was signed with an Apple Development
 * certificate that `spctl` rejects for exactly the same reason (not a
 * Developer ID) but which is at least structurally sound.
 *
 * WHY A HOOK AND NOT A CONFIG FLAG
 * --------------------------------
 * electron-builder has no "ad-hoc sign" setting. `mac.identity: null` and
 * CSC_IDENTITY_AUTO_DISCOVERY=false both mean SKIP, which is the broken state
 * above. Doing it in `afterPack` runs after the .app is fully packed but
 * before the DMG is assembled, so the DMG contains the signed bundle, and it
 * behaves identically on a laptop and on a GitHub runner because it depends on
 * no keychain state whatsoever.
 *
 * This file lives in `macOS/scripts/` rather than `macOS/build/` only because
 * the repo's root .gitignore ignores every directory named `build`. It is
 * build-time tooling and is excluded from the shipped asar by the `!scripts`
 * entry in the `files` array in package.json.
 *
 * WHY IT THROWS
 * -------------
 * The defect that produced this blocker was a skipped signing step that still
 * exited 0, so every downstream signal looked fine. This hook verifies its own
 * work and throws on failure, making an unsignable bundle a loud build failure
 * instead of a DMG that only misbehaves on the user's machine.
 */

const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

/**
 * Run a command, returning stdout as a string. Throws on non-zero exit with
 * stderr attached, since every caller here treats failure as fatal.
 *
 * @param {string} cmd Executable name.
 * @param {string[]} args Arguments.
 * @returns {string} stdout, trimmed.
 */
function run(cmd, args) {
  return execFileSync(cmd, args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
}

/**
 * Ad-hoc sign a macOS bundle and prove the signature is structurally valid.
 *
 * `--deep` is deprecated by Apple for signing real identities because it does
 * not apply per-target entitlements, but for an identity-less ad-hoc pass over
 * an Electron tree (frameworks, helper apps, dylibs) it is the correct tool:
 * there are no entitlements to get wrong. Hardened runtime is deliberately NOT
 * enabled (`--options runtime` is absent) because it would demand entitlements
 * for V8's JIT that an ad-hoc signature cannot usefully carry.
 *
 * @param {string} appPath Absolute path to the .app bundle.
 * @returns {void}
 * @throws {Error} If signing or verification fails, or _CodeSignature is absent.
 */
function adhocSign(appPath) {
  run('codesign', ['--force', '--deep', '--sign', '-', '--timestamp=none', appPath]);

  // Proof 1: the sealed-resources directory must now exist. Its absence was
  // the entire failure mode, so check the artifact directly, not the exit code.
  const sigDir = path.join(appPath, 'Contents', '_CodeSignature');
  if (!fs.existsSync(path.join(sigDir, 'CodeResources'))) {
    throw new Error(
      `ad-hoc signing reported success but ${sigDir}/CodeResources does not exist. ` +
        'The bundle would show "damaged and can\'t be opened" once quarantined.'
    );
  }

  // Proof 2: the signature verifies strictly, including nested code.
  run('codesign', ['--verify', '--deep', '--strict', '--verbose=2', appPath]);
}

/**
 * electron-builder afterPack entry point.
 *
 * @param {object} context electron-builder hook context.
 * @param {string} context.appOutDir Directory holding the packed .app.
 * @param {string} context.electronPlatformName Platform being packed.
 * @param {object} context.packager Packager, used for the product filename.
 * @returns {Promise<void>}
 */
module.exports = async function adhocSignHook(context) {
  if (context.electronPlatformName !== 'darwin') {
    return;
  }

  const appName = `${context.packager.appInfo.productFilename}.app`;
  const appPath = path.join(context.appOutDir, appName);

  if (!fs.existsSync(appPath)) {
    throw new Error(`afterPack: expected bundle not found at ${appPath}`);
  }

  console.log(`[adhoc-sign] signing ${appPath}`);
  adhocSign(appPath);
  console.log('[adhoc-sign] signature present and verified (ad-hoc, no identity)');
};

// Exported for the unit test, which checks the hook's contract without
// running a full electron-builder pack.
module.exports.adhocSign = adhocSign;
