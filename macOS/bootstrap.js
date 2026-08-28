/**
 * bootstrap.js — First-run provisioning state machine for Cloude Code macOS app.
 *
 * Goal: on first launch after install, self-provision the server environment
 * under ~/Library/Application Support/<productName>/server/ so the user never
 * has to touch a terminal. Subsequent launches fast-path through (< 50ms) by
 * verifying a venv + .env + deps-hash sentinel trio.
 *
 * State machine surfaced via onStateChange(state) callback so main.js can
 * update tray tooltip text (the only UI surface during bootstrap — no modals,
 * no progress bars, no toasts. Menu-bar apps stay out of the way).
 *
 * States (string enum, observer pattern):
 *   'checking'            — verifying Python + existing sentinel files
 *   'python-missing'      — Python 3.12+ absent; caller must show dialog + quit
 *   'syncing-assets'      — rsync bundled build artifacts → Application Support
 *                           (runs on EVERY packaged launch so upgrades land)
 *   'preparing'           — creating Application Support directory tree
 *   'copying-files'       — bundled resources → Application Support (first run)
 *   'creating-venv'       — python3 -m venv
 *   'installing-deps'     — venv/bin/pip install -r requirements.txt
 *   'generating-secrets'  — TOTP_SECRET + JWT_SECRET into .env
 *   'generating-config'   — config.example.json → config.json
 *   'ready'               — all good, server can start
 *   'error'               — unrecoverable; caller must show errorBox + quit
 *
 * Return shape: { status, freshInstall, details? }
 *   - status:       'ready' | 'python-missing' | 'error'
 *   - freshInstall: true if we generated new secrets this run (main.js
 *                   uses this to auto-pop the TOTP QR window after startup)
 *   - details:      human-readable error string when status === 'error' or
 *                   'python-missing'
 */

const { spawn, execFile, spawnSync } = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const os = require('os');

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

// Minimum Python version. Server code uses 3.12 syntax (e.g. PEP 695 generics
// in some modules). Be strict here so we fail fast with a clear message
// instead of cryptic ImportError at server startup.
const MIN_PYTHON_MAJOR = 3;
const MIN_PYTHON_MINOR = 12;

// GUI-launched macOS apps inherit /usr/bin:/bin:/usr/sbin:/sbin — no Homebrew.
// Every spawn must inject a real PATH or `python3` / `pip` won't be found.
const SAFE_PATH = [
  '/opt/homebrew/bin',   // Apple Silicon Homebrew
  '/usr/local/bin',      // Intel Homebrew
  '/usr/bin',
  '/bin',
  '/usr/sbin',
  '/sbin',
].join(':');

// RFC 4648 base32 alphabet (for TOTP_SECRET — pyotp-compatible)
const BASE32_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

/**
 * Encode a Buffer as RFC 4648 base32 (no padding, uppercase).
 * pyotp.random_base32() produces exactly this format with 20 bytes → 32 chars.
 */
function base32Encode(buffer) {
  let bits = 0;
  let value = 0;
  let output = '';
  for (const byte of buffer) {
    value = (value << 8) | byte;
    bits += 8;
    while (bits >= 5) {
      output += BASE32_ALPHABET[(value >>> (bits - 5)) & 0x1f];
      bits -= 5;
    }
  }
  if (bits > 0) {
    output += BASE32_ALPHABET[(value << (5 - bits)) & 0x1f];
  }
  return output;
}

/**
 * Build a clean env dict for spawning Python/pip subprocesses.
 * Preserves HOME, USER, LANG, etc. but forces a working PATH.
 */
function spawnEnv(extra = {}) {
  return {
    ...process.env,
    PATH: `${SAFE_PATH}:${process.env.PATH || ''}`,
    ...extra,
  };
}

/**
 * Run a command, capturing stdout+stderr. Returns { code, stdout, stderr }.
 * Never rejects — caller inspects code.
 */
function runCommand(cmd, args, opts = {}) {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, {
      env: spawnEnv(opts.env),
      cwd: opts.cwd,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => {
      const s = d.toString();
      stdout += s;
      if (opts.onStdout) opts.onStdout(s);
    });
    child.stderr.on('data', (d) => {
      const s = d.toString();
      stderr += s;
      if (opts.onStderr) opts.onStderr(s);
    });
    child.on('error', (err) => {
      resolve({ code: -1, stdout, stderr: stderr + `\n[spawn error] ${err.message}` });
    });
    child.on('exit', (code) => {
      resolve({ code: code ?? -1, stdout, stderr });
    });
  });
}

/**
 * Locate a python3 binary on the system with version >= 3.12.
 * Returns { path, version } on success, null if no suitable Python found.
 *
 * We check candidates in priority order (Homebrew first because it's the
 * canonical modern Python on macOS) and verify each by running --version
 * and parsing the output.
 */
async function findSuitablePython() {
  const candidates = [
    '/opt/homebrew/bin/python3.12',
    '/opt/homebrew/bin/python3.13',
    '/opt/homebrew/bin/python3',
    '/usr/local/bin/python3.12',
    '/usr/local/bin/python3.13',
    '/usr/local/bin/python3',
    path.join(os.homedir(), '.pyenv', 'shims', 'python3'),
    '/usr/bin/python3',
  ];

  for (const candidate of candidates) {
    if (!fs.existsSync(candidate)) continue;
    const { code, stdout, stderr } = await runCommand(candidate, ['--version']);
    if (code !== 0) continue;
    const out = (stdout + stderr).trim();
    const match = out.match(/Python (\d+)\.(\d+)\.(\d+)/);
    if (!match) continue;
    const major = parseInt(match[1], 10);
    const minor = parseInt(match[2], 10);
    if (major > MIN_PYTHON_MAJOR || (major === MIN_PYTHON_MAJOR && minor >= MIN_PYTHON_MINOR)) {
      return { path: candidate, version: `${match[1]}.${match[2]}.${match[3]}` };
    }
  }
  return null;
}

/**
 * Recursively copy a directory. Skips entries that already exist at dest
 * (idempotent — safe to re-run). Returns number of files copied.
 */
function copyRecursive(src, dest) {
  let copied = 0;
  if (!fs.existsSync(src)) return 0;
  if (!fs.existsSync(dest)) fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const s = path.join(src, entry.name);
    const d = path.join(dest, entry.name);
    if (entry.isDirectory()) {
      copied += copyRecursive(s, d);
    } else if (!fs.existsSync(d)) {
      fs.copyFileSync(s, d);
      copied++;
    }
  }
  return copied;
}

/**
 * sha256 of a file's bytes. Returns hex digest.
 */
function sha256File(filePath) {
  const hash = crypto.createHash('sha256');
  hash.update(fs.readFileSync(filePath));
  return hash.digest('hex');
}

/**
 * Atomic file write: write to .tmp, then rename.
 * Prevents a half-written .env if the process dies mid-write.
 */
function atomicWriteFile(filePath, content) {
  const tmp = filePath + '.tmp';
  fs.writeFileSync(tmp, content, 'utf8');
  fs.renameSync(tmp, filePath);
}

/**
 * Check if a path is executable by the current user.
 */
function isExecutable(p) {
  try {
    fs.accessSync(p, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// .env generation
// ---------------------------------------------------------------------------

/**
 * Generate the initial .env file by taking .env.example verbatim and
 * substituting in fresh secrets + sensible defaults. NEVER overwrites an
 * existing .env — the user may have customized it.
 *
 * Substitutions (via line-prefix match, preserves original order):
 *   TOTP_SECRET=<32-char base32>
 *   JWT_SECRET=<64-char hex>
 *   DEFAULT_WORKING_DIR=<expanded ~/cloude-projects>
 *   LOG_DIRECTORY=<expanded ~/Library/Logs/cloude-code>
 *
 * The Cloudflare section stays empty — that's already the default state in
 * the example, and LAN-only (local_only tunnel backend) is the ship default.
 */
function generateEnvFile({ envExamplePath, envOutPath, defaultWorkingDir, logDirectory }) {
  if (!fs.existsSync(envExamplePath)) {
    throw new Error(`.env.example not found at ${envExamplePath}`);
  }

  const totpSecret = base32Encode(crypto.randomBytes(20));  // 32 chars
  const jwtSecret = crypto.randomBytes(32).toString('hex');  // 64 chars

  const lines = fs.readFileSync(envExamplePath, 'utf8').split('\n');
  const out = lines.map((line) => {
    if (line.startsWith('TOTP_SECRET=')) return `TOTP_SECRET=${totpSecret}`;
    if (line.startsWith('JWT_SECRET=')) return `JWT_SECRET=${jwtSecret}`;
    if (line.startsWith('DEFAULT_WORKING_DIR=')) return `DEFAULT_WORKING_DIR=${defaultWorkingDir}`;
    if (line.startsWith('LOG_DIRECTORY=')) return `LOG_DIRECTORY=${logDirectory}`;
    return line;
  });

  atomicWriteFile(envOutPath, out.join('\n'));
  return { totpSecretLen: totpSecret.length, jwtSecretLen: jwtSecret.length };
}

// ---------------------------------------------------------------------------
// Bundle → userData asset resync (runs on EVERY packaged launch)
// ---------------------------------------------------------------------------

/**
 * Allowlist of build artifacts that ship inside the .app bundle and must
 * track whatever version of the app is currently launching. On every
 * packaged launch we rsync these from <bundle>/Resources/<name> into
 * <serverDir>/<name>, deleting orphans from within those paths.
 *
 * The test for membership is NOT "is it a file we shipped" but "does the
 * running app depend on this being the CURRENT version" - which includes
 * every script the app executes by path (see nuke.sh below).
 *
 * EVERYTHING ELSE in serverDir is user-owned state and MUST NOT be touched:
 *   - .env                      (user secrets + config)
 *   - config.json               (user-customized runtime config)
 *   - venv/                     (python virtualenv — big, expensive to rebuild)
 *   - .deps-hash                (cache key for pip reinstall decision)
 *   - refresh_tokens.db         (auth state)
 *   - session_metadata.json     (tmux session bookkeeping)
 *   - logs/                     (runtime logs)
 *   - any future user-created files
 *
 * This is an ALLOWLIST by design: anything we don't explicitly name is
 * preserved. Defense against future drift — if a new user-state file
 * appears, it's safe by default.
 *
 * Dirs use rsync -a --delete with trailing slashes (contents sync, not
 * parent-dir nesting). Files are plain copyFile (overwrite). macOS ships
 * with rsync in /usr/bin/rsync — always available, no dep issue.
 */
const RESYNC_ALLOWLIST = [
  { name: 'src', isDir: true },
  { name: 'client', isDir: true },
  { name: 'requirements.txt', isDir: false },
  { name: 'setup_auth.py', isDir: false },
  { name: 'config.example.json', isDir: false },
  { name: '.env.example', isDir: false },
  // nuke.sh is EXECUTED from the derived tree by macOS/main.js
  // (path.join(serverManager.getProjectRoot(), 'nuke.sh')), so it is a build
  // artifact of the running version, not user state. It used to be first-run
  // copy only, on the theory that a user might have customized their copy.
  // That traded a hypothetical customization against a real one: every
  // UPGRADE got the NEW typed-NUKE confirmation window wired to the OLD
  // destructive script - the one that missed the state directory and left
  // cloude.db and refresh_tokens.db on disk while reporting a full reset.
  // Anything the app invokes by path must track the app. Guarded by
  // tests/test_runtime_script_delivery.py.
  { name: 'nuke.sh', isDir: false },
];

/**
 * rsync a single directory, with trailing-slash semantics:
 *   rsync -a --delete SRC/ DST/   (sync CONTENTS; --delete wipes orphans in DST)
 *
 * Returns { ok: true } on success or { ok: false, code, stderr } on failure.
 * Caller decides whether non-zero is fatal (it should be — a half-synced
 * tree is worse than no sync).
 */
function rsyncDir(srcDir, dstDir) {
  // Ensure dst parent exists; rsync will create dstDir itself, but mkdir
  // is free insurance against edge cases where dstDir's parent doesn't exist.
  fs.mkdirSync(dstDir, { recursive: true });
  const result = spawnSync(
    '/usr/bin/rsync',
    ['-a', '--delete', `${srcDir}/`, `${dstDir}/`],
    { encoding: 'utf8' }
  );
  if (result.status !== 0) {
    return {
      ok: false,
      code: result.status,
      stderr: (result.stderr || '').trim(),
    };
  }
  return { ok: true };
}

/**
 * Re-sync bundled build artifacts into serverDir. Runs on every packaged
 * launch so version upgrades land without manual intervention.
 *
 * In dev mode (!isPackaged), this is a no-op — the dev's working tree
 * is authoritative and we don't want to rsync a stale .app bundle's
 * Resources over live source edits.
 *
 * Returns { ok: true, summary: string } on success,
 *         { ok: false, details: string } on any rsync/copy failure.
 */
function syncBundledAssets({ serverDir, bundleResourcesDir, isPackaged }) {
  if (!isPackaged) {
    console.log('[bootstrap] dev mode — skipping bundled-asset resync');
    return { ok: true, summary: 'dev-mode skip' };
  }

  if (!fs.existsSync(bundleResourcesDir)) {
    return {
      ok: false,
      details: `bundleResourcesDir does not exist: ${bundleResourcesDir}`,
    };
  }

  fs.mkdirSync(serverDir, { recursive: true });

  const t0 = Date.now();
  const synced = [];
  const skipped = [];

  for (const item of RESYNC_ALLOWLIST) {
    const src = path.join(bundleResourcesDir, item.name);
    const dst = path.join(serverDir, item.name);

    if (!fs.existsSync(src)) {
      // Artifact missing from bundle — don't blow up (older/newer bundle
      // layouts might omit an entry), just log and move on. This is
      // distinct from a rsync failure, which IS fatal.
      skipped.push(`${item.name} (not in bundle)`);
      continue;
    }

    if (item.isDir) {
      const res = rsyncDir(src, dst);
      if (!res.ok) {
        return {
          ok: false,
          details: `rsync failed for ${item.name}/ (exit ${res.code}): ${res.stderr.slice(-400)}`,
        };
      }
      synced.push(`${item.name}/`);
    } else {
      // File: overwrite unconditionally. copyFileSync is atomic-ish on
      // macOS (single write to a freshly-opened fd) — good enough for
      // small config files. We don't preserve the prior content because
      // these are all example/template files the user shouldn't edit
      // in-place anyway (they edit config.json / .env derived from them).
      //
      // The executable bit survives: on macOS copyFileSync goes through
      // copyfile(3) and applies the SOURCE mode even when the destination
      // already exists with a different one. Measured 2026-08-21, not
      // assumed - nuke.sh and setup_auth.py both have to stay 0755 here or
      // main.js's exec of the copied path fails with EACCES.
      try {
        fs.copyFileSync(src, dst);
        synced.push(item.name);
      } catch (err) {
        return {
          ok: false,
          details: `copy failed for ${item.name}: ${err.message}`,
        };
      }
    }
  }

  const elapsed = Date.now() - t0;
  const summary =
    `resynced ${synced.length} items in ${elapsed}ms ` +
    `[${synced.join(', ')}]` +
    (skipped.length ? ` skipped [${skipped.join(', ')}]` : '');
  console.log(`[bootstrap] ${summary}`);
  return { ok: true, summary };
}

// ---------------------------------------------------------------------------
// User themes dir provisioning (Phase 9 — pluggability surface)
//
// Creates ~/Library/Application Support/cloude-code-menubar/themes/ if it
// doesn't exist, then drops a README.md ONCE so the user has a starting
// schema reference. The README is written ONLY when absent — preserving
// any user edits across upgrades.
//
// This dir lives OUTSIDE the resync allowlist (RESYNC_ALLOWLIST in
// syncBundledAssets) so DMG upgrades will never blow away user themes.
//
// Idempotent and safe to call on every launch. Failures are logged but
// non-fatal: a missing themes dir simply means the FastAPI /themes mount
// is skipped (server logs "user_themes_mount_skipped") and the discovery
// endpoint returns bundled-only.
// ---------------------------------------------------------------------------

const USER_THEMES_README = `# Cloude Code — User Themes

Drop your custom themes in this directory as subfolders, one per theme.
Each theme is a folder named after the theme \`id\`, containing a
\`theme.json\` manifest and (optionally) an \`effects.js\` for runtime FX.

## Discovery

The server scans this dir on every \`GET /api/v1/themes\` call. No restart
needed — just refresh the browser after dropping a new theme in.

If a user theme has the same \`id\` as a bundled theme, the bundled one
wins and the user theme is skipped with a server-log warning.

## Schema

A minimal \`theme.json\`:

\`\`\`json
{
  "id": "neon",
  "name": "Neon",
  "description": "Pink-on-black retro arcade vibe.",
  "author": "you",
  "version": "1.0.0",
  "cssVars": {
    "--color-bg": "#0a0014",
    "--color-fg": "#ff00ff",
    "--color-accent": "#00ffff",
    "--color-border": "#330033"
  },
  "xterm": {
    "background": "#0a0014",
    "foreground": "#ff00ff",
    "cursor": "#00ffff"
  },
  "effects": "effects.js"
}
\`\`\`

## Fields

- \`id\` — must match the folder name. Mismatched manifests are skipped.
- \`name\`, \`description\` — shown in the theme selector.
- \`cssVars\` — map of CSS custom-property name to value, applied to \`:root\`.
- \`xterm\` — xterm.js theme object (background/foreground/ANSI palette).
- \`effects\` — optional filename of a JS module relative to the theme dir.
  First time a theme with effects is applied, the UI prompts for consent
  (Allow once / Always / Never). Choices persist in localStorage.

## Security

Theme assets are served unauth at \`http://<server>/themes/<id>/<file>\`.
Do NOT put secrets in \`theme.json\` or \`effects.js\`. Same threat model
as any static resource on the LAN-only deployment.
`;

function provisionUserThemesDir() {
  const dir = path.join(
    os.homedir(),
    'Library',
    'Application Support',
    'cloude-code-menubar',
    'themes'
  );
  try {
    if (!fs.existsSync(dir)) {
      fs.mkdirSync(dir, { recursive: true });
      console.log(`[bootstrap] created user themes dir: ${dir}`);
    }
    const readmePath = path.join(dir, 'README.md');
    if (!fs.existsSync(readmePath)) {
      fs.writeFileSync(readmePath, USER_THEMES_README, 'utf8');
      console.log(`[bootstrap] dropped user themes README: ${readmePath}`);
    }
    return { ok: true, dir };
  } catch (e) {
    // Non-fatal: server will simply skip the /themes mount and serve
    // bundled-only. Surface the warning so it's visible in Electron logs.
    console.warn(`[bootstrap] could not provision user themes dir: ${e.message}`);
    return { ok: false, dir, error: e.message };
  }
}

// ---------------------------------------------------------------------------
// VERSION stamp
// ---------------------------------------------------------------------------

/**
 * Header written into serverDir/VERSION. Must stay byte-identical to
 * VERSION_FILE_HEADER in src/core/version.py, which is the reader.
 * @type {string}
 */
const VERSION_FILE_HEADER =
  '# GENERATED FROM THE GIT TAG AT RELEASE TIME. DO NOT EDIT BY HAND.\n' +
  '# See src/core/version.py for the resolution order.\n';

/**
 * Stamp the app version into serverDir/VERSION.
 *
 * WHY THIS EXISTS. The Python server resolves its version through
 * src/core/version.py, whose first source is the CLOUDE_APP_VERSION env var
 * that server-manager.js injects at spawn. That covers the normal path and
 * nothing else: serverDir is a plain copy of src/ + client/ with NO .git and
 * NO macOS/package.json, so a server started any other way - by hand, by a
 * future wrapper, by a debugging session - resolves to "" and the release
 * self check correctly but uselessly reports "unknown". Writing the file the
 * resolver already looks for second closes that on disk, where it survives.
 *
 * @param {string} serverDir - Application Support/.../server dir.
 * @param {string} version - the app version, from app.getVersion().
 * @returns {boolean} true when written; false on any failure (non-fatal:
 *   the env var still covers the spawn path).
 */
function writeVersionStamp(serverDir, version) {
  const value = String(version || '').trim();
  if (!value) return false;
  try {
    fs.writeFileSync(
      path.join(serverDir, 'VERSION'),
      `${VERSION_FILE_HEADER}${value}\n`,
      'utf8'
    );
    console.log(`[bootstrap] stamped VERSION=${value}`);
    return true;
  } catch (err) {
    console.warn(`[bootstrap] could not stamp VERSION: ${err.message}`);
    return false;
  }
}

// ---------------------------------------------------------------------------
// Main bootstrap orchestrator
// ---------------------------------------------------------------------------

/**
 * @param {Object} opts
 * @param {string} opts.serverDir      - Target Application Support/server/ dir
 * @param {string} opts.bundleResourcesDir - Source: packaged app Resources/
 * @param {boolean} [opts.isPackaged]  - true in packaged .app, false in `npm start`
 *                                       dev mode. If omitted, auto-detected from
 *                                       bundleResourcesDir path (contains
 *                                       '.app/Contents/Resources').
 * @param {Function} opts.onStateChange - (state: string) => void — state observer
 * @param {string} [opts.appVersion]   - app.getVersion(), stamped into
 *                                       serverDir/VERSION so the Python
 *                                       resolver has an on-disk answer.
 * @returns {Promise<{status: string, freshInstall: boolean, details?: string}>}
 */
async function bootstrapIfNeeded({
  serverDir, bundleResourcesDir, isPackaged, onStateChange, appVersion,
}) {
  const emit = (state) => {
    if (onStateChange) {
      try { onStateChange(state); } catch (_) { /* observer errors are non-fatal */ }
    }
  };

  // Auto-detect packaged mode if caller didn't pass it. Reliable heuristic on
  // macOS: Electron's process.resourcesPath in packaged mode always lives
  // under `<something>.app/Contents/Resources`. In dev (`npm start`) the
  // caller typically passes a path like `<repo>/macOS/..` — not a .app.
  const packaged = typeof isPackaged === 'boolean'
    ? isPackaged
    : /\.app\/Contents\/Resources\/?$/.test(bundleResourcesDir || '');

  emit('checking');

  // -------------------------------------------------------------------------
  // 1. Python 3.12+ detection
  // -------------------------------------------------------------------------
  const python = await findSuitablePython();
  if (!python) {
    return {
      status: 'python-missing',
      freshInstall: false,
      details: `Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ not found on PATH.`,
    };
  }
  console.log(`[bootstrap] Python found: ${python.path} (${python.version})`);

  // -------------------------------------------------------------------------
  // 1a. User themes dir (Phase 9)
  //
  // Idempotent: creates the dir + drops README only when missing. Runs on
  // every launch so a user who wipes the dir manually gets a fresh README
  // next boot. Failures here are non-fatal — server tolerates absence.
  // -------------------------------------------------------------------------
  provisionUserThemesDir();

  // -------------------------------------------------------------------------
  // 1b. Bundle → userData resync (EVERY launch in packaged mode)
  //
  // This is the upgrade path. The .app bundle ships fresh src/ + client/ +
  // requirements.txt etc. under Contents/Resources. We rsync those into
  // serverDir so the running version of the app always serves the client
  // files it was built with. Runs BEFORE the fast-path check on purpose:
  //   - requirements.txt getting overwritten may change its sha256, which
  //     the downstream deps-hash check will catch and trigger a pip reinstall.
  //   - A changed client/ needs to land even when everything else is already
  //     provisioned (.env, venv, config.json all exist from prior launch).
  //
  // DEV MODE RESYNCS TOO, and the comment that used to sit here claiming
  // otherwise had the direction of the copy backwards. It read "don't
  // clobber live source tree with stale bundle" - but in dev
  // `bundleResourcesDir` IS the live source tree (main.js: `path.join
  // (__dirname, '..')`, the repo root) and serverDir is the DERIVED copy
  // under Application Support. The rsync runs repo -> derived. It cannot
  // reach the source tree at all.
  //
  // What the skip actually did: the only thing landing files in dev was
  // the first-run `copyRecursive` below, which SKIPS any file that
  // already exists at the destination. So a NEW file appeared on the next
  // launch and a CHANGED file never did, forever. Measured 2026-08-20:
  // the user was testing a serverDir client/ whose index.html predated
  // the merge, with none of the new screen-chrome files present, while
  // the checkout on disk was correct - so half his UI was one build and
  // half was another, and nothing anywhere errored. He reported it as a
  // rendering defect, which is exactly what it looked like.
  //
  // The version stamp stays packaged-only: it records which RELEASE
  // provisioned serverDir, and a dev launch is not a release.
  // -------------------------------------------------------------------------
  {
    emit('syncing-assets');
    // Ensure serverDir exists before rsync so we never hit a missing-parent
    // race on a brand-new install where the fast-path check below would
    // normally create it via 'preparing'.
    fs.mkdirSync(serverDir, { recursive: true });
    // `isPackaged: true` is passed unconditionally on purpose. Inside
    // syncBundledAssets that flag means only "do the resync"; the dev
    // launch wants exactly the same resync, from the repo root instead
    // of from Contents/Resources. It is not a claim about how the app
    // was launched, and nothing downstream reads it as one.
    const sync = syncBundledAssets({
      serverDir, bundleResourcesDir, isPackaged: true
    });
    if (!sync.ok) {
      // A failed resync is NOT recoverable — a half-synced client/ dir will
      // serve a mix of old + new files and break the app in confusing ways.
      // Surface the error loudly.
      return {
        status: 'error',
        freshInstall: false,
        details: `asset resync failed: ${sync.details}`,
      };
    }
    // Stamp AFTER the resync: the resync does not carry a VERSION file, and
    // an upgrade must overwrite the previous release's stamp.
    if (packaged) {
      writeVersionStamp(serverDir, appVersion);
    }
  }

  // -------------------------------------------------------------------------
  // 2. Fast path: is everything already provisioned?
  // -------------------------------------------------------------------------
  const venvPython = path.join(serverDir, 'venv', 'bin', 'python3');
  const envPath = path.join(serverDir, '.env');
  const depsHashPath = path.join(serverDir, '.deps-hash');
  const requirementsPath = path.join(serverDir, 'requirements.txt');
  const configPath = path.join(serverDir, 'config.json');

  const fastPathOk = (
    isExecutable(venvPython) &&
    fs.existsSync(envPath) &&
    /^TOTP_SECRET=.+$/m.test(fs.readFileSync(envPath, 'utf8')) &&
    fs.existsSync(configPath) &&
    fs.existsSync(requirementsPath) &&
    fs.existsSync(depsHashPath) &&
    fs.readFileSync(depsHashPath, 'utf8').trim() === sha256File(requirementsPath)
  );

  if (fastPathOk) {
    emit('ready');
    return { status: 'ready', freshInstall: false };
  }

  // -------------------------------------------------------------------------
  // 3. Slow path: provision what's missing
  // -------------------------------------------------------------------------
  emit('preparing');

  try {
    if (!fs.existsSync(serverDir)) {
      fs.mkdirSync(serverDir, { recursive: true });
    }

    // 3a. Copy bundled resources → serverDir
    //
    // In PACKAGED mode the resync step above (1b) has already pulled in all
    // allowlisted build artifacts (src/, client/, requirements.txt, setup_auth.py,
    // config.example.json, .env.example). Here we only handle:
    //   - DEV-mode first-run copy (resync is skipped in dev — we still need
    //     the artifacts landed the first time the dev launches into a clean
    //     serverDir, otherwise the venv/deps/env steps have nothing to key off).
    //
    // nuke.sh USED to be handled here and only here (first-run copy, never
    // refreshed). It is now in RESYNC_ALLOWLIST like every other executed
    // artifact, so this list is simply the allowlist itself - one declaration,
    // not two that can drift apart.
    emit('copying-files');
    // In packaged mode these are already resynced above; copyRecursive and the
    // skip-if-exists file branch make this a no-op in that case. In dev mode,
    // this is the first-run landing.
    const firstRunResources = RESYNC_ALLOWLIST;
    for (const item of firstRunResources) {
      const src = path.join(bundleResourcesDir, item.name);
      const dst = path.join(serverDir, item.name);
      if (!fs.existsSync(src)) {
        // A bundled resource can legitimately be absent in a partial dev
        // checkout. Skip rather than abort; the resync path is authoritative.
        console.warn(`[bootstrap] bundled resource missing (skipping): ${src}`);
        continue;
      }
      if (item.isDir) {
        const n = copyRecursive(src, dst);
        if (n > 0) console.log(`[bootstrap] copied ${n} files into ${item.name}/`);
      } else if (!fs.existsSync(dst)) {
        fs.copyFileSync(src, dst);
        console.log(`[bootstrap] copied ${item.name}`);
      }
    }

    // 3b. Create venv if missing
    const venvDir = path.join(serverDir, 'venv');
    if (!isExecutable(venvPython)) {
      emit('creating-venv');
      console.log(`[bootstrap] creating venv at ${venvDir}`);
      const { code, stderr } = await runCommand(python.path, ['-m', 'venv', venvDir]);
      if (code !== 0 || !isExecutable(venvPython)) {
        return {
          status: 'error',
          freshInstall: false,
          details: `python3 -m venv failed (exit ${code}):\n${stderr.slice(-600)}`,
        };
      }
    }

    // 3c. Install deps if requirements.txt changed or .deps-hash missing
    if (!fs.existsSync(requirementsPath)) {
      return {
        status: 'error',
        freshInstall: false,
        details: `requirements.txt missing after copy step — bundled resource not found at ${path.join(bundleResourcesDir, 'requirements.txt')}`,
      };
    }
    const currentHash = sha256File(requirementsPath);
    const previousHash = fs.existsSync(depsHashPath)
      ? fs.readFileSync(depsHashPath, 'utf8').trim()
      : '';
    if (currentHash !== previousHash) {
      emit('installing-deps');
      console.log('[bootstrap] installing pip requirements (this may take 60-120s on first run)');
      const pipPath = path.join(venvDir, 'bin', 'pip');
      const { code, stderr } = await runCommand(
        pipPath,
        ['install', '--disable-pip-version-check', '-r', requirementsPath],
        {
          onStdout: (chunk) => {
            // Log each line to Electron console for debug visibility
            for (const line of chunk.split('\n')) {
              if (line.trim()) console.log(`[pip] ${line.trim()}`);
            }
          },
          onStderr: (chunk) => {
            for (const line of chunk.split('\n')) {
              if (line.trim()) console.log(`[pip!] ${line.trim()}`);
            }
          },
        }
      );
      if (code !== 0) {
        return {
          status: 'error',
          freshInstall: false,
          details: `pip install failed (exit ${code}):\n${stderr.slice(-600)}`,
        };
      }
      fs.writeFileSync(depsHashPath, currentHash, 'utf8');
      console.log(`[bootstrap] deps-hash written: ${currentHash.slice(0, 12)}...`);
    }

    // 3d. Generate .env if missing (NEVER overwrite existing)
    let freshInstall = false;
    if (!fs.existsSync(envPath)) {
      emit('generating-secrets');
      const defaultWorkingDir = path.join(os.homedir(), 'cloude-projects');
      const logDirectory = path.join(os.homedir(), 'Library', 'Logs', 'cloude-code');

      // Ensure user-facing directories exist (server's Settings class requires
      // LOG_DIRECTORY to be writable at startup; DEFAULT_WORKING_DIR is where
      // new projects get scaffolded)
      for (const d of [defaultWorkingDir, logDirectory]) {
        try {
          if (!fs.existsSync(d)) fs.mkdirSync(d, { recursive: true });
        } catch (e) {
          console.warn(`[bootstrap] could not create ${d}: ${e.message}`);
        }
      }

      const envExamplePath = path.join(serverDir, '.env.example');
      const { totpSecretLen, jwtSecretLen } = generateEnvFile({
        envExamplePath,
        envOutPath: envPath,
        defaultWorkingDir,
        logDirectory,
      });
      console.log(`[bootstrap] .env generated (TOTP_SECRET len ${totpSecretLen}, JWT_SECRET len ${jwtSecretLen})`);
      freshInstall = true;
    }

    // 3e. Generate config.json if missing
    if (!fs.existsSync(configPath)) {
      emit('generating-config');
      const exampleConfig = path.join(serverDir, 'config.example.json');
      if (!fs.existsSync(exampleConfig)) {
        return {
          status: 'error',
          freshInstall: false,
          details: `config.example.json missing after copy — bundled resource absent`,
        };
      }
      fs.copyFileSync(exampleConfig, configPath);
      console.log('[bootstrap] config.json created from example');
    }

    emit('ready');
    return { status: 'ready', freshInstall };
  } catch (err) {
    console.error('[bootstrap] unhandled error:', err);
    return {
      status: 'error',
      freshInstall: false,
      details: err && err.stack ? err.stack : String(err),
    };
  }
}

module.exports = {
  bootstrapIfNeeded,
  // Exported for unit-testing / debug:
  base32Encode,
  findSuitablePython,
  sha256File,
  syncBundledAssets,
  provisionUserThemesDir,
  writeVersionStamp,
  VERSION_FILE_HEADER,
  RESYNC_ALLOWLIST,
};
