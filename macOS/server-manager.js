const { spawn, exec } = require('child_process');
const path = require('path');
const axios = require('axios');
const { app } = require('electron');
const net = require('net');
const fs = require('fs');
const os = require('os');

// Default bind host = 0.0.0.0 (listen on all interfaces). Matches the
// pydantic Settings default in src/config.py; changing here without
// changing there would cause a silent drift.
const DEFAULT_BIND_HOST = '0.0.0.0';

// macOS pseudo-interfaces that should NEVER appear in the Bind IP submenu.
// awdl/llw = AirDrop/Apple Wireless Direct Link (link-local IPv6 only).
// utun = VPN tunnels (link-local IPv6 usually; user-bound VPN, not a LAN
// binding target). anpi/ap1 = internal radios on Apple Silicon. Skip them
// wholesale by name — they never carry routable IPv4 even if one shows up.
const PSEUDO_IFACE_PATTERNS = [
  /^awdl/i, /^llw/i, /^utun/i, /^anpi/i, /^ap\d/i,
];

class ServerManager {
  constructor() {
    this.process = null;
    this.processPid = null;
    this.ownedProcess = false; // true if we spawned the server, false if adopted
    this.logStream = null;

    // Determine base directory based on whether app is packaged
    if (app.isPackaged) {
      // In production: Store server files in Application Support
      // This makes the app portable across different machines
      this.baseDir = path.join(app.getPath('userData'), 'server');
      this.appResourcesPath = path.join(app.getAppPath(), '..');
    } else {
      // In development: running from macOS/ folder
      this.baseDir = path.join(__dirname, '..');
      this.appResourcesPath = this.baseDir;
    }

    // Auto-detect Python installation
    this.pythonPath = this.findPython();
    this.apiUrl = 'http://127.0.0.1:8000';
    this.port = 8000;
    // Use userData/logs for persistent logging
    const logDir = path.join(app.getPath('userData'), 'logs');
    if (!fs.existsSync(logDir)) {
      fs.mkdirSync(logDir, { recursive: true });
    }
    this.logFile = path.join(logDir, 'server.log');
    this.state = 'stopped'; // 'stopped', 'starting', 'running'
    this.startTime = null;

    // Settings file for persistent menu-bar prefs (currently only bind_host).
    // Lives in userData root — small, atomic-written, one object deep.
    this.settingsPath = path.join(app.getPath('userData'), 'menubar-settings.json');
    this._settings = this._loadSettings();
  }

  // ---------------------------------------------------------------------
  // Settings persistence (menubar-settings.json)
  // ---------------------------------------------------------------------

  /**
   * Load settings from disk. Returns an object with defaults filled in.
   * Silently recovers from missing/corrupt file by returning defaults.
   */
  _loadSettings() {
    const defaults = { bind_host: DEFAULT_BIND_HOST };
    try {
      if (!fs.existsSync(this.settingsPath)) return defaults;
      const raw = fs.readFileSync(this.settingsPath, 'utf8');
      const parsed = JSON.parse(raw);
      return { ...defaults, ...parsed };
    } catch (err) {
      console.warn('[settings] could not load, using defaults:', err.message);
      return defaults;
    }
  }

  /**
   * Atomic write of settings to disk (write-temp-then-rename).
   */
  _saveSettings() {
    try {
      const tmp = this.settingsPath + '.tmp';
      fs.writeFileSync(tmp, JSON.stringify(this._settings, null, 2), 'utf8');
      fs.renameSync(tmp, this.settingsPath);
    } catch (err) {
      console.error('[settings] save failed:', err.message);
    }
  }

  /**
   * Current bind host ('0.0.0.0' | '127.0.0.1' | specific LAN IP).
   */
  getBindHost() {
    return this._settings.bind_host || DEFAULT_BIND_HOST;
  }

  /**
   * Persist a new bind host and trigger a server restart so uvicorn
   * re-binds. No-op if the value is unchanged.
   */
  async setBindHost(ip) {
    if (!ip || typeof ip !== 'string') {
      throw new Error(`invalid bind host: ${ip}`);
    }
    if (this._settings.bind_host === ip) {
      console.log(`[bind-host] already set to ${ip}, no-op`);
      return;
    }
    console.log(`[bind-host] change: ${this._settings.bind_host} -> ${ip}`);
    this._settings.bind_host = ip;
    this._saveSettings();

    // Full restart is required — uvicorn binds at startup and has no
    // in-place rebind. tmux sessions survive because the tmux server is
    // a separate daemon (session_manager rehydrates on lifespan_startup).
    await this.restart();
  }

  /**
   * Enumerate IPv4 LAN interface IPs suitable for binding.
   * Filters out: loopback (internal), link-local 169.254.x, and macOS
   * pseudo-interfaces (awdl/llw/utun/anpi/ap).
   * Returns: [{iface: 'en0', ip: '192.168.1.250'}, ...]
   */
  getLocalInterfaceIps() {
    const results = [];
    let ifaces;
    try {
      ifaces = os.networkInterfaces();
    } catch (err) {
      console.warn('[bind-host] networkInterfaces() failed:', err.message);
      return [];
    }
    for (const [name, addrs] of Object.entries(ifaces || {})) {
      if (PSEUDO_IFACE_PATTERNS.some((rx) => rx.test(name))) continue;
      if (!Array.isArray(addrs)) continue;
      for (const a of addrs) {
        if (!a || a.family !== 'IPv4') continue;
        if (a.internal) continue;
        if (typeof a.address !== 'string') continue;
        if (a.address.startsWith('169.254.')) continue; // link-local
        results.push({ iface: name, ip: a.address });
      }
    }
    // Stable ordering by interface name (en0 before en13 etc.)
    results.sort((a, b) => a.iface.localeCompare(b.iface));
    return results;
  }

  /**
   * Primary LAN IP — first non-loopback IPv4 from getLocalInterfaceIps().
   * Used to resolve "0.0.0.0" into a copyable URL. Returns null if no
   * LAN iface is available (airgapped Mac) — caller falls back to 127.
   */
  getPrimaryLanIp() {
    const ips = this.getLocalInterfaceIps();
    return ips.length > 0 ? ips[0].ip : null;
  }

  /**
   * Compute the currently-reachable URL for this server based on bind
   * host. Tunnel URL resolution is not done here because the /tunnels
   * endpoint is auth-gated and the Electron app has no JWT — users with
   * tunnels active get the LAN URL as a sensible fallback.
   */
  getPublishedUrl() {
    const host = this.getBindHost();
    if (host === '0.0.0.0') {
      const lan = this.getPrimaryLanIp();
      return `http://${lan || '127.0.0.1'}:${this.port}`;
    }
    return `http://${host}:${this.port}`;
  }

  /**
   * Find Python 3 installation on the system
   * @returns {string} Path to python3 executable
   */
  findPython() {
    // In development, prefer local venv
    if (!app.isPackaged) {
      const localVenv = path.join(this.baseDir, 'venv', 'bin', 'python3');
      if (fs.existsSync(localVenv)) {
        console.log(`Using local venv Python: ${localVenv}`);
        return localVenv;
      }
    }

    // Check common Python installation locations
    const pythonLocations = [
      '/opt/homebrew/bin/python3',     // Apple Silicon Homebrew
      '/usr/local/bin/python3',        // Intel Homebrew
      '/usr/bin/python3',              // System Python
      path.join(process.env.HOME || '', '.pyenv', 'shims', 'python3'), // pyenv
    ];

    for (const location of pythonLocations) {
      if (fs.existsSync(location)) {
        console.log(`Found Python at: ${location}`);
        return location;
      }
    }

    // Fallback to PATH
    console.log('Using python3 from PATH');
    return 'python3';
  }

  /**
   * Ensure cloudflared is installed
   */
  async ensureCloudflared() {
    const binDir = path.join(this.baseDir, 'bin');
    const cloudflaredPath = path.join(binDir, 'cloudflared');

    // Check if already exists
    if (fs.existsSync(cloudflaredPath)) {
      console.log('cloudflared found at:', cloudflaredPath);
      return cloudflaredPath;
    }

    // Check if in global PATH
    try {
      const { stdout } = await new Promise((resolve) => exec('which cloudflared', (err, stdout) => resolve({ stdout })));
      if (stdout && stdout.trim()) {
        console.log('cloudflared found in PATH:', stdout.trim());
        return 'cloudflared';
      }
    } catch (e) {
      // Ignore
    }

    console.log('cloudflared not found, downloading...');

    if (!fs.existsSync(binDir)) {
      fs.mkdirSync(binDir, { recursive: true });
    }

    const arch = process.arch === 'arm64' ? 'arm64' : 'amd64';
    const url = `https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-${arch}.tgz`;

    console.log(`Downloading from ${url}...`);

    try {
      const response = await axios({
        url,
        method: 'GET',
        responseType: 'stream'
      });

      const tgzPath = path.join(binDir, 'cloudflared.tgz');
      const writer = fs.createWriteStream(tgzPath);

      await new Promise((resolve, reject) => {
        response.data.pipe(writer);
        writer.on('finish', resolve);
        writer.on('error', reject);
      });

      console.log('Download complete, extracting...');

      await new Promise((resolve, reject) => {
        exec(`tar -xzf "${tgzPath}" -C "${binDir}"`, (err) => {
          if (err) reject(err);
          else resolve();
        });
      });

      // Cleanup tgz
      fs.unlinkSync(tgzPath);

      // Ensure executable
      fs.chmodSync(cloudflaredPath, '755');

      console.log('cloudflared installed successfully');
      return cloudflaredPath;

    } catch (err) {
      console.error('Failed to download cloudflared:', err);
      throw new Error('Failed to download cloudflared: ' + err.message);
    }
  }

  /**
   * Ensure server files exist in baseDir
   * Copies from bundled resources if needed (packaged app)
   */
  async ensureServerFiles() {
    const requiredDirs = ['src', 'client'];
    const requiredFiles = ['setup_auth.py', 'requirements.txt', 'config.example.json', 'nuke.sh', '.env.example'];

    // Create baseDir if it doesn't exist
    if (!fs.existsSync(this.baseDir)) {
      console.log(`Creating baseDir: ${this.baseDir}`);
      fs.mkdirSync(this.baseDir, { recursive: true });
    }

    if (app.isPackaged) {
      // Copy files from app resources to Application Support
      const resourcesPath = this.appResourcesPath;

      for (const dir of requiredDirs) {
        const srcPath = path.join(resourcesPath, dir);
        const destPath = path.join(this.baseDir, dir);

        if (fs.existsSync(srcPath) && !fs.existsSync(destPath)) {
          console.log(`Copying ${dir}/ to Application Support...`);
          this.copyRecursive(srcPath, destPath);
        }
      }

      for (const file of requiredFiles) {
        const srcPath = path.join(resourcesPath, file);
        const destPath = path.join(this.baseDir, file);

        if (fs.existsSync(srcPath) && !fs.existsSync(destPath)) {
          console.log(`Copying ${file} to Application Support...`);
          fs.copyFileSync(srcPath, destPath);
        }
      }
    }

    // Check if essential files exist
    const srcDir = path.join(this.baseDir, 'src');
    if (!fs.existsSync(srcDir)) {
      throw new Error(`Server source files not found at: ${srcDir}`);
    }
  }

  /**
   * Copy directory recursively
   */
  copyRecursive(src, dest) {
    if (!fs.existsSync(dest)) {
      fs.mkdirSync(dest, { recursive: true });
    }

    const entries = fs.readdirSync(src, { withFileTypes: true });

    for (const entry of entries) {
      const srcPath = path.join(src, entry.name);
      const destPath = path.join(dest, entry.name);

      if (entry.isDirectory()) {
        this.copyRecursive(srcPath, destPath);
      } else {
        fs.copyFileSync(srcPath, destPath);
      }
    }
  }

  /**
   * Ensure virtual environment exists and has dependencies installed
   */
  async ensureVenv() {
    const venvPath = path.join(this.baseDir, 'venv');
    const requirementsPath = path.join(this.baseDir, 'requirements.txt');

    // If venv doesn't exist, create it
    if (!fs.existsSync(venvPath)) {
      console.log('Creating virtual environment...');

      return new Promise((resolve, reject) => {
        exec(`"${this.pythonPath}" -m venv "${venvPath}"`, (error) => {
          if (error) {
            console.error('Failed to create venv:', error);
            reject(error);
            return;
          }

          // Install requirements
          if (fs.existsSync(requirementsPath)) {
            const venvPython = path.join(venvPath, 'bin', 'python3');
            console.log('Installing requirements...');

            exec(`"${venvPython}" -m pip install -r "${requirementsPath}"`, (error2) => {
              if (error2) {
                console.error('Failed to install requirements:', error2);
                reject(error2);
                return;
              }

              console.log('Venv setup complete');
              // Update pythonPath to use venv
              this.pythonPath = venvPython;
              resolve();
            });
          } else {
            resolve();
          }
        });
      });
    } else {
      // Venv exists, use it
      const venvPython = path.join(venvPath, 'bin', 'python3');
      if (fs.existsSync(venvPython)) {
        this.pythonPath = venvPython;
        console.log(`Using existing venv: ${venvPython}`);
      }
    }
  }

  /**
   * Check if port is in use
   * @returns {Promise<boolean>}
   */
  async isPortInUse() {
    return new Promise((resolve) => {
      const server = net.createServer();

      server.once('error', (err) => {
        if (err.code === 'EADDRINUSE') {
          resolve(true);
        } else {
          resolve(false);
        }
      });

      server.once('listening', () => {
        server.close();
        resolve(false);
      });

      server.listen(this.port, '0.0.0.0');
    });
  }

  /**
   * Start the Python FastAPI server
   */
  async start() {
    if (this.process) {
      console.log('Server already running');
      return;
    }

    // First-run setup: Ensure server files, venv, and cloudflared exist
    try {
      await this.ensureServerFiles();
      await this.ensureVenv();
      await this.ensureCloudflared();
    } catch (error) {
      console.error('Setup failed:', error);
      this.state = 'stopped';
      throw error;
    }

    // Validate .env file has required fields
    const validation = this.validateEnvFile();
    if (!validation.isValid) {
      const errorMsg = `Configuration validation failed:\n${validation.errors.join('\n')}`;
      console.error(errorMsg);

      // Write to startup error log even if server doesn't start
      const errorLogPath = path.join(app.getPath('userData'), 'logs', 'startup-errors.log');
      try {
        const timestamp = new Date().toISOString();
        fs.appendFileSync(errorLogPath, `\n[${timestamp}] ${errorMsg}\n`);
        console.log(`Error logged to: ${errorLogPath}`);
      } catch (logErr) {
        console.warn('Could not write to error log:', logErr.message);
      }

      this.state = 'stopped';
      throw new Error(errorMsg);
    }

    // Check if port is already in use
    const portInUse = await this.isPortInUse();
    if (portInUse) {
      console.log(`Port ${this.port} already in use, checking if it's our server...`);
      const health = await this.getHealth();
      if (health) {
        console.log('Server already running on port, adopting it');
        this.state = 'running';
        this.startTime = Date.now(); // Approximate
        this.ownedProcess = false; // We didn't spawn this — don't kill it on quit

        // Try to capture the PID of the existing process (for display only)
        try {
          exec(`lsof -ti:${this.port}`, (err, stdout) => {
            if (!err && stdout) {
              const pid = parseInt(stdout.trim());
              if (!isNaN(pid)) {
                console.log(`Adopted existing server process PID: ${pid}`);
                this.processPid = pid;
              }
            }
          });
        } catch (e) {
          console.warn('Could not determine PID of running server:', e.message);
        }

        return;
      } else {
        console.error(`Port ${this.port} in use by another process!`);
        return;
      }
    }

    console.log('Starting Cloude Code server...');
    console.log(`Base directory: ${this.baseDir}`);
    console.log(`Python path: ${this.pythonPath}`);
    console.log(`Log file: ${this.logFile}`);

    this.state = 'starting';

    // Create log file stream
    this.logStream = fs.createWriteStream(this.logFile, { flags: 'a' });
    this.logStream.write(`\n\n=== Server starting at ${new Date().toISOString()} ===\n`);

    // Add bin directory to PATH so python process can find cloudflared.
    // Also prepend Homebrew bin dirs so the Python server can locate tmux
    // under Electron's launchd environment (which strips user PATH).
    // /opt/homebrew/bin = Apple Silicon, /usr/local/bin = Intel Homebrew.
    const binDir = path.join(this.baseDir, 'bin');
    const env = { ...process.env };
    env.PATH = `${binDir}:/opt/homebrew/bin:/usr/local/bin:${env.PATH}`;

    // Inject the bind host. pydantic-settings in src/config.py is
    // case-insensitive, so plain HOST maps to settings.host which
    // uvicorn.run reads in src/main.py. Always set explicitly — never
    // rely on the .env baseline — so this is the single source of truth.
    env.HOST = this.getBindHost();
    console.log(`[spawn] HOST=${env.HOST}`);

    this.process = spawn(this.pythonPath, ['-m', 'src.main'], {
      cwd: this.baseDir,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: env
    });

    this.processPid = this.process.pid;
    this.ownedProcess = true; // We spawned it — we can safely kill it on quit
    this.startTime = Date.now();
    console.log(`Server process started with PID: ${this.processPid}`);

    // Log stdout and detect when server is ready
    this.process.stdout.on('data', (data) => {
      const output = data.toString().trim();
      console.log(`[SERVER] ${output}`);

      // Write to log file
      if (this.logStream) {
        this.logStream.write(`[STDOUT] ${output}\n`);
      }

      // Check if server is ready
      if (output.includes('Application startup complete') ||
        output.includes('application_ready')) {
        this.state = 'running';
      }
    });

    // Log stderr
    this.process.stderr.on('data', (data) => {
      const output = data.toString().trim();
      console.error(`[SERVER ERROR] ${output}`);

      // Write to log file
      if (this.logStream) {
        this.logStream.write(`[STDERR] ${output}\n`);
      }

      // Also check stderr for ready signal
      if (output.includes('Application startup complete')) {
        this.state = 'running';
      }
    });

    // Handle process exit
    this.process.on('exit', (code, signal) => {
      console.log(`Server process exited with code ${code} and signal ${signal}`);

      // Close log stream
      if (this.logStream) {
        this.logStream.write(`\n=== Server stopped at ${new Date().toISOString()} (code: ${code}, signal: ${signal}) ===\n`);
        this.logStream.end();
        this.logStream = null;
      }

      this.process = null;
      this.processPid = null;
      this.ownedProcess = false;
      this.state = 'stopped';
      this.startTime = null;
    });

    // Handle process errors
    this.process.on('error', (err) => {
      console.error('Failed to start server:', err);

      // Close log stream
      if (this.logStream) {
        this.logStream.write(`\n=== Server error: ${err.message} ===\n`);
        this.logStream.end();
        this.logStream = null;
      }

      this.process = null;
      this.processPid = null;
      this.ownedProcess = false;
      this.state = 'stopped';
      this.startTime = null;
    });

    console.log('Server process started');
  }

  /**
   * Kill process by PID
   */
  killByPid(pid, signal = 'SIGTERM') {
    return new Promise((resolve) => {
      exec(`kill -${signal === 'SIGTERM' ? '15' : '9'} ${pid}`, (error) => {
        if (error) {
          console.log(`Failed to kill PID ${pid}:`, error.message);
        }
        resolve();
      });
    });
  }

  /**
   * Kill any process using port 8000
   */
  killByPort() {
    return new Promise((resolve) => {
      exec(`lsof -ti:${this.port} | xargs kill -9`, (error) => {
        if (error) {
          console.log('No process found on port', this.port);
        } else {
          console.log('Killed process on port', this.port);
        }
        resolve();
      });
    });
  }

  /**
   * Stop the server.
   *
   * Only stops servers we own (spawned). Adopted servers are left alone —
   * we don't own them, we don't kill them. Shutdown is process-signal based
   * (SIGTERM → 3s grace → SIGKILL). No HTTP shutdown call — the /api/v1/shutdown
   * endpoint now requires auth, and signaling a PID we own is strictly simpler.
   */
  async stop() {
    console.log('Stopping server...');

    // Adopted server: we didn't start it, we don't stop it.
    if (!this.ownedProcess) {
      console.log('Server was adopted, not owned — leaving it running.');
      // Clear our local references so menu reflects "stopped" from app's POV.
      this.processPid = null;
      this.state = 'stopped';
      this.startTime = null;
      return;
    }

    // Owned server: SIGTERM via process ref or PID, then SIGKILL after grace period.
    if (this.process && !this.process.killed) {
      console.log('Sending SIGTERM to owned server process...');
      this.process.kill('SIGTERM');

      // Give uvicorn ~3s to flush connections, then force kill.
      await new Promise(resolve => setTimeout(resolve, 3000));

      if (this.process && !this.process.killed) {
        console.log('Server did not exit on SIGTERM, sending SIGKILL');
        this.process.kill('SIGKILL');
      }
    } else if (this.processPid) {
      // Process object lost but we have PID (edge case: app restart mid-lifecycle).
      console.log(`Sending SIGTERM to owned PID ${this.processPid}...`);
      await this.killByPid(this.processPid, 'SIGTERM');
      await new Promise(resolve => setTimeout(resolve, 3000));
      await this.killByPid(this.processPid, 'SIGKILL');
    }

    // Close log stream
    if (this.logStream) {
      this.logStream.end();
      this.logStream = null;
    }

    // Wait briefly for exit event to fire, then clean up state if it didn't.
    await new Promise(resolve => setTimeout(resolve, 500));

    if (!this.process) {
      this.processPid = null;
      this.ownedProcess = false;
      this.state = 'stopped';
      this.startTime = null;
    }
  }

  /**
   * Restart the server
   */
  async restart() {
    console.log('Restarting server...');
    await this.stop();

    // Wait a bit before restarting
    await new Promise(resolve => setTimeout(resolve, 3000));

    await this.start();
    console.log('Server restarted');
  }

  /**
   * Check if server is healthy and get stats
   * @returns {Promise<Object|null>} Server stats or null if unhealthy
   */
  async getHealth() {
    try {
      const response = await axios.get(`${this.apiUrl}/api/v1/health`, {
        timeout: 3000
      });
      return response.data;
    } catch (err) {
      // Server not responding
      return null;
    }
  }

  /**
   * Get server uptime in seconds
   * @returns {number} Uptime in seconds
   */
  getUptime() {
    if (!this.startTime) return 0;
    return Math.floor((Date.now() - this.startTime) / 1000);
  }

  /**
   * Get current server state
   * @returns {string} 'stopped', 'starting', or 'running'
   */
  getState() {
    return this.state;
  }

  /**
   * Check if process is running
   * @returns {boolean}
   */
  isProcessRunning() {
    // If we spawned the process ourselves, check the process object
    if (this.process && !this.process.killed) {
      return true;
    }

    // If we have a PID (either spawned or adopted), verify it's still alive
    if (this.processPid) {
      try {
        // Sending signal 0 doesn't actually send a signal, just checks if process exists
        process.kill(this.processPid, 0);
        return true;
      } catch (err) {
        if (err.code === 'ESRCH') {
          // No such process - it died
          this.processPid = null;
          return false;
        }
        // Other error (e.g., permission denied) - assume it's not running
        console.warn('Error checking process PID:', err.message);
        return false;
      }
    }

    return false;
  }

  /**
   * Validate .env file has all required fields for server startup
   * @returns {Object} Validation result with isValid flag and error details
   */
  validateEnvFile() {
    const envPath = path.join(this.baseDir, '.env');

    const result = {
      isValid: true,
      missingRequired: [],
      emptyRequired: [],
      errors: []
    };

    // Check if .env exists
    if (!fs.existsSync(envPath)) {
      result.isValid = false;
      result.errors.push('.env file not found. Run setup first.');
      return result;
    }

    // Read .env content
    const envContent = fs.readFileSync(envPath, 'utf8');

    // CRITICAL: These fields are required by Settings class (no defaults)
    const criticalFields = ['DEFAULT_WORKING_DIR', 'LOG_DIRECTORY'];

    // IMPORTANT: These fields are required for authentication
    const authFields = ['TOTP_SECRET', 'JWT_SECRET'];

    // Check critical fields
    criticalFields.forEach(field => {
      const regex = new RegExp(`^${field}=(.*)$`, 'm');
      const match = envContent.match(regex);

      if (!match) {
        result.missingRequired.push(field);
        result.isValid = false;
      } else if (!match[1] || match[1].trim() === '') {
        result.emptyRequired.push(field);
        result.isValid = false;
      }
    });

    // Check auth fields
    authFields.forEach(field => {
      const regex = new RegExp(`^${field}=(.*)$`, 'm');
      const match = envContent.match(regex);

      if (!match) {
        result.missingRequired.push(field);
        result.isValid = false;
      } else if (!match[1] || match[1].trim() === '') {
        result.emptyRequired.push(field);
        result.isValid = false;
      }
    });

    // Build error messages
    if (result.missingRequired.length > 0) {
      result.errors.push(`Missing required fields: ${result.missingRequired.join(', ')}`);
    }
    if (result.emptyRequired.length > 0) {
      result.errors.push(`Empty required fields: ${result.emptyRequired.join(', ')}`);
    }

    return result;
  }

  /**
   * Check if configuration is complete
   * @returns {Object} Status object with isConfigured flag and details
   */
  checkConfiguration() {
    const envPath = path.join(this.baseDir, '.env');
    const configPath = path.join(this.baseDir, 'config.json');
    const setupScriptPath = path.join(this.baseDir, 'setup_auth.py');

    const status = {
      isConfigured: true,
      missingFiles: [],
      missingEnvVars: [],
      details: []
    };

    // Check if .env exists
    if (!fs.existsSync(envPath)) {
      status.isConfigured = false;
      status.missingFiles.push('.env');
      status.details.push('.env file not found');
    } else {
      // Check required env vars
      const envContent = fs.readFileSync(envPath, 'utf8');
      const requiredVars = [
        'TOTP_SECRET',
        'JWT_SECRET',
        'CLOUDFLARE_API_TOKEN',
        'CLOUDFLARE_ZONE_ID',
        'CLOUDFLARE_DOMAIN'
      ];

      requiredVars.forEach(varName => {
        // Check if var exists and has a non-empty value
        const regex = new RegExp(`${varName}=(.+)`, 'm');
        const match = envContent.match(regex);

        if (!match || !match[1] || match[1].trim() === '' || match[1].trim() === '""') {
          status.isConfigured = false;
          status.missingEnvVars.push(varName);
        }

        // Check for placeholder values in CLOUDFLARE_DOMAIN
        if (varName === 'CLOUDFLARE_DOMAIN' && match && match[1]) {
          const domain = match[1].trim();
          if (domain.includes('example.com') ||
            domain.includes('yourdomain.com') ||
            domain.includes('your-subdomain') ||
            domain.includes('mydomain.nyc')) {
            status.isConfigured = false;
            status.details.push('CLOUDFLARE_DOMAIN contains placeholder value. Run setup to configure.');
          }
        }
      });

      if (status.missingEnvVars.length > 0) {
        status.details.push(`Missing env vars: ${status.missingEnvVars.join(', ')}`);
      }
    }

    // Check if config.json exists
    if (!fs.existsSync(configPath)) {
      status.missingFiles.push('config.json');
      status.details.push('config.json not found (optional)');
    }

    // Check if setup script exists
    if (!fs.existsSync(setupScriptPath)) {
      status.details.push('setup_auth.py not found');
    }

    return status;
  }

  /**
   * Open Terminal and run setup script
   */
  openSetupScript() {
    const setupScript = path.join(this.baseDir, 'setup_auth.py');
    const pythonPath = this.pythonPath;

    // Open Terminal and run setup
    exec(`osascript -e 'tell application "Terminal" to do script "cd \\"${this.baseDir}\\" && \\"${pythonPath}\\" setup_auth.py"'`);
  }

  /**
   * Get the project root directory
   * @returns {string} Path to project root
   */
  getProjectRoot() {
    return this.baseDir;
  }
}

module.exports = ServerManager;
