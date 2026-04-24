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
 *   'preparing'           — creating Application Support directory tree
 *   'copying-files'       — bundled resources → Application Support
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

const { spawn, execFile } = require('child_process');
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
// Main bootstrap orchestrator
// ---------------------------------------------------------------------------

/**
 * @param {Object} opts
 * @param {string} opts.serverDir      - Target Application Support/server/ dir
 * @param {string} opts.bundleResourcesDir - Source: packaged app Resources/
 * @param {Function} opts.onStateChange - (state: string) => void — state observer
 * @returns {Promise<{status: string, freshInstall: boolean, details?: string}>}
 */
async function bootstrapIfNeeded({ serverDir, bundleResourcesDir, onStateChange }) {
  const emit = (state) => {
    if (onStateChange) {
      try { onStateChange(state); } catch (_) { /* observer errors are non-fatal */ }
    }
  };

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
    emit('copying-files');
    const resourceMap = [
      { name: 'src', isDir: true },
      { name: 'client', isDir: true },
      { name: 'requirements.txt', isDir: false },
      { name: 'setup_auth.py', isDir: false },
      { name: 'config.example.json', isDir: false },
      { name: '.env.example', isDir: false },
      { name: 'nuke.sh', isDir: false },
    ];
    for (const item of resourceMap) {
      const src = path.join(bundleResourcesDir, item.name);
      const dst = path.join(serverDir, item.name);
      if (!fs.existsSync(src)) {
        // Some items (nuke.sh) may be optional. Skip if not bundled.
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
};
