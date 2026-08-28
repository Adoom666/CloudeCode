const { app, Tray, Menu, shell, nativeImage, clipboard, dialog, nativeTheme } = require('electron');
const path = require('path');
const ServerManager = require('./server-manager');
const LaunchAgentInstaller = require('./launchagent-installer');
const { bootstrapIfNeeded } = require('./bootstrap');
const terminalLauncher = require('./terminal-launcher');
const trayStatus = require('./tray-status');
const { TrayApiClient } = require('./tray-api');
const { isTailscaleIp } = require('./network-interfaces');
const tlsStatus = require('./tls-status');
const { createQuitHandler } = require('./quit-sequence');

// ---------------------------------------------------------------------------
// Menu-bar-only presence (no Dock icon)
//
// Two halves, and BOTH are required:
//   * `LSUIElement` in macOS/package.json build.mac.extendInfo stops a PACKAGED
//     build from ever registering a Dock tile. Without it the tile appears at
//     launch and only disappears once JS runs, which is a visible bounce.
//   * `app.dock.hide()` here covers a dev run (`electron .`), where the plist
//     that applies is Electron's own, not ours.
//
// Cost of hiding: the process becomes NSApplicationActivationPolicyAccessory,
// which is NOT a normal activatable app. A window it opens can land behind
// whatever the user was using, and a modeless dialog can do the same. Electron
// gives no automatic activation for an accessory app, so every surface that
// becomes visible has to ask for the app to be brought forward explicitly.
//
// This is done in ONE place rather than at each of the ~13 call sites so that
// a surface added later cannot forget it. `browser-window-created` catches
// every BrowserWindow, and the dialog wrappers catch every dialog, including
// ones this file does not yet contain.
// ---------------------------------------------------------------------------

/**
 * Remove the Dock tile for this process. macOS only; a no-op elsewhere.
 *
 * Inputs: none
 * Outputs: boolean - true if the Dock tile was hidden, false if there was no
 *          Dock to hide (non-darwin) or the call failed.
 */
function hideFromDock() {
  if (process.platform !== 'darwin' || !app.dock) return false;
  try {
    app.dock.hide();
    return true;
  } catch (err) {
    // Not fatal: the app still works, it just keeps a Dock tile. Say so
    // rather than swallowing it, because the symptom (a Dock icon) looks
    // like the feature was never implemented.
    console.warn('[dock] could not hide Dock tile:', err.message);
    return false;
  }
}

/**
 * Bring this process to the front. Required before showing any window or
 * dialog once the Dock tile is gone, because an accessory app is never
 * activated for you.
 *
 * Inputs: none
 * Outputs: void
 */
function activateForForeground() {
  try {
    app.focus({ steal: true });
  } catch (err) {
    console.warn('[dock] could not activate app:', err.message);
  }
}

/**
 * Wrap dialog.showErrorBox / showMessageBox / showMessageBoxSync so each one
 * activates the app first. Mutates the properties of the shared `dialog`
 * object, so later `require('electron').dialog` destructures in this file see
 * the wrapped versions too (same object, resolved at call time).
 *
 * Inputs: none
 * Outputs: void
 */
function installDialogActivation() {
  for (const name of ['showErrorBox', 'showMessageBox', 'showMessageBoxSync']) {
    const original = dialog[name];
    if (typeof original !== 'function' || original.__frontsApp) continue;
    const wrapped = function (...args) {
      activateForForeground();
      return original.apply(dialog, args);
    };
    wrapped.__frontsApp = true;
    dialog[name] = wrapped;
  }
}

/**
 * Make every BrowserWindow front the app when it becomes visible.
 *
 * Inputs: none
 * Outputs: void
 */
function installWindowActivation() {
  app.on('browser-window-created', (_event, win) => {
    win.on('show', () => {
      activateForForeground();
      try {
        win.moveTop();
        win.focus();
      } catch (err) {
        console.warn('[dock] could not front window:', err.message);
      }
    });
  });
}

hideFromDock();
installDialogActivation();
installWindowActivation();


let tray = null;
let serverManager = null;
let launchAgentInstaller = null;
let statsUpdateInterval = null;
let currentStats = null;

// Tray status-light state (see tray-status.js). `traySignals` holds the last
// answers from the server. sessionsReachable STARTS FALSE on purpose: before
// the first successful poll the app has not measured anything, and an
// unmeasured tray must not render as healthy.
let trayApi = null;
let trayPollInterval = null;
let currentTrayState = null;
const traySignals = {
  sessions: null,
  sessionsReachable: false,
  sessionsError: 'not polled yet',
  updateStatus: null,
  updateReachable: false,
  latestVersion: null,
};

// Human-readable labels for each bootstrap state, surfaced via tray tooltip.
// Keep these concise — the tooltip is the ONLY UI surface during first-run
// provisioning (no modals, no toasts; this is a menu-bar app).
const BOOTSTRAP_TOOLTIPS = {
  'checking': 'Cloude Code — checking setup...',
  'syncing-assets': 'Cloude Code — syncing bundled files...',
  'preparing': 'Cloude Code — preparing first-run...',
  'copying-files': 'Cloude Code — copying server files...',
  'creating-venv': 'Cloude Code — creating Python venv...',
  'installing-deps': 'Cloude Code — installing dependencies (60-120s)...',
  'generating-secrets': 'Cloude Code — generating auth secrets...',
  'generating-config': 'Cloude Code — writing config...',
  'ready': 'Cloude Code',
};

/**
 * Show the TOTP QR pairing window by fetching it live from the running
 * server. Used for both the manual menu action and the auto-pop on fresh
 * installs. Swallows errors silently — caller is responsible for surfacing
 * them if this is user-initiated.
 */
/**
 * Read TOTP_SECRET out of the server's own .env.
 *
 * Same machine, same user, mode 0600. Returns null rather than throwing:
 * a missing or unreadable .env is a state the caller reports, not an
 * exception to propagate out of a menu handler.
 *
 * NEVER LOGGED, and never used anywhere except the otpauth URI that
 * becomes the QR.
 */
function readTotpSecret(serverDir) {
  const fs = require('fs');
  const path = require('path');
  try {
    const text = fs.readFileSync(path.join(serverDir, '.env'), 'utf8');
    for (const line of text.split('\n')) {
      const m = /^\s*TOTP_SECRET\s*=\s*(.+?)\s*$/.exec(line);
      if (m) return m[1].replace(/^['"]|['"]$/g, '');
    }
    return null;
  } catch (err) {
    return null;
  }
}

async function showQrPairingWindow() {
  const { BrowserWindow, dialog } = require('electron');

  try {
    // GENERATED LOCALLY, NOT FETCHED. This used to GET
    // /api/v1/auth/qr, which carried three problems at once:
    //
    //   1. That endpoint is UNAUTHENTICATED by necessity - it is what
    //      you use before you have a TOTP - while the server binds
    //      0.0.0.0. It therefore refuses to serve the secret once
    //      pairing is complete (403), which is correct, and which also
    //      means the pairing window could not be reopened without
    //      exposing the secret to anything able to reach port 8000.
    //   2. It returned a 510x510 PNG that the window scaled to 320px, a
    //      0.6275 factor, so every QR module landed on a fractional
    //      pixel and smooth downscaling blurred the edges into grey -
    //      measured at 8.2% of pixels in the mid-grey band, which is
    //      exactly what a scanner cannot binarise.
    //   3. It moved a secret across a socket between two processes on
    //      the same machine.
    //
    // Rendering here fixes all three. The secret is read from the
    // server's own .env and never crosses a network boundary, so the
    // 403 guard can stay shut permanently. The output is SVG: no raster
    // to resample, exact at any size, and the blur cannot return the way
    // it would after a pixel-size tweak.
    const QRCode = require('qrcode');
    // baseDir is the server directory - the same one getConfigPath()
    // derives config.json from. There is no getServerDir(); asking for
    // one returns undefined and this would read '/.env' silently.
    const secret = readTotpSecret(serverManager.baseDir);
    if (!secret) {
      throw new Error(
        'TOTP secret not readable from the server .env - has setup run?'
      );
    }
    const issuer = encodeURIComponent('Cloude Code');
    const qrSvg = await QRCode.toString(
      `otpauth://totp/${issuer}:${issuer}?secret=${secret}&issuer=${issuer}`,
      { type: 'svg', margin: 2, errorCorrectionLevel: 'M',
        color: { dark: '#000000', light: '#ffffff' } }
    );
    const qrWindow = new BrowserWindow({
      width: 420,
      height: 520,
      resizable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      show: false,
      backgroundColor: '#1a1a1a',
      title: 'Cloude Code — Pair Your Authenticator',
      webPreferences: {
        nodeIntegration: false,
        contextIsolation: true,
        sandbox: true
      }
    });
    const html = `
      <!DOCTYPE html><html><head><meta charset="utf-8"><style>
        body{margin:0;padding:32px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
          background:linear-gradient(135deg,#1a1a1a 0%,#2d2d2d 100%);color:#fff;display:flex;
          flex-direction:column;align-items:center;justify-content:center;height:100vh;box-sizing:border-box;}
        h1{margin:0 0 8px 0;font-size:22px;font-weight:600;color:#CC785C;}
        p{margin:0 0 20px 0;font-size:13px;color:#999;text-align:center;max-width:340px;line-height:1.5;}
        /* WHY image-rendering AND box-sizing ARE BOTH LOAD-BEARING.
         *
         * The server renders this QR at 510x510 (a 103-char otpauth URI
         * fits as version 6 = 41 modules, plus a 5-module quiet zone each
         * side, at box_size 10). Forcing it to 320px is a 0.6275 scale,
         * so every module lands on a fractional pixel boundary at 6.27px
         * and the browser's default SMOOTH downscale antialiases each
         * module edge into grey. That is what made this QR unreadable to
         * phone scanners: not a wrong code, a blurred one.
         *
         * image-rendering pixelated switches the resample to
         * nearest-neighbour, so module edges stay hard even at a
         * non-integer scale - which is what a scanner's binarisation
         * step needs.
         *
         * box-sizing border-box is set because the 14px padding IS the
         * white quiet zone: without it the element is 320 + 28 = 348px
         * and overflows the 420px window's 32px body padding. The body
         * sets border-box for itself only, and box-sizing does not
         * inherit.
         *
         * NOTE TO THE NEXT EDITOR: no backticks in this comment. It sits
         * inside a JS template literal, so one would terminate the
         * string - which is exactly what happened when it was written. */
        /* SVG now, so no image-rendering hint is needed: there is no
         * raster to resample and the code is exact at any size. The old
         * PNG needed image-rendering:pixelated purely to survive a
         * non-integer downscale. box-sizing stays - the 14px padding IS
         * the white quiet zone and must sit inside the 320px rather than
         * be added to it; body sets border-box for itself only. */
        .qr{width:320px;height:320px;background:#fff;border-radius:12px;padding:14px;
          box-sizing:border-box;box-shadow:0 8px 32px rgba(0,0,0,0.4);}
        .qr svg{width:100%;height:100%;display:block;}
        .footer{margin-top:20px;font-size:11px;color:#666;}
      </style></head><body>
        <h1>☁️ Welcome to Cloude Code</h1>
        <p>Scan this QR with Google Authenticator, 1Password, Authy — any TOTP app.</p>
        <div class="qr" role="img" aria-label="TOTP QR code">${qrSvg}</div>
        <div class="footer">Paired already? You can close this window.</div>
      </body></html>`;
    qrWindow.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
    qrWindow.once('ready-to-show', () => qrWindow.show());
    qrWindow.setMenu(null);
  } catch (err) {
    console.warn('[first-run] could not auto-show QR:', err.message);
  }
}

/**
 * Poll health endpoint until server is ready or timeout.
 */
async function waitForServerHealth(timeoutMs = 30000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const health = await serverManager.getHealth();
    if (health) return true;
    await new Promise(r => setTimeout(r, 500));
  }
  return false;
}

/**
 * Show About dialog with app info and GitHub link
 */
function showAboutDialog() {
  const { BrowserWindow } = require('electron');

  // Create a small modal window.
  // Uses Electron-recommended secure defaults: no node integration, context isolation on.
  // The window HTML is static display only — no Node APIs needed. External links
  // are intercepted via setWindowOpenHandler below and routed through shell.openExternal.
  const aboutWindow = new BrowserWindow({
    width: 500,
    height: 400,
    resizable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    show: false,
    backgroundColor: '#1a1a1a',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true
    }
  });

  // Intercept target="_blank" / window.open and route to external browser
  aboutWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  // Get the icon path and convert to data URL for reliable display
  // In packaged app, assets are in app.asar, not Resources
  const iconPath = app.isPackaged
    ? path.join(app.getAppPath(), 'assets', 'AppIcon-1024.png')
    : path.join(__dirname, 'assets', 'AppIcon-1024.png');

  // Read image file and convert to base64 data URL
  const fs = require('fs');
  const iconBuffer = fs.readFileSync(iconPath);
  const iconBase64 = iconBuffer.toString('base64');
  const iconDataUrl = `data:image/png;base64,${iconBase64}`;

  const currentYear = new Date().getFullYear();
  const appVersion = `v${app.getVersion()}`;

  // HTML content for the about dialog
  const html = `
    <!DOCTYPE html>
    <html>
    <head>
      <style>
        body {
          margin: 0;
          padding: 40px;
          font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Roboto', sans-serif;
          background: linear-gradient(135deg, #1a1a1a 0%, #2d2d2d 100%);
          color: #ffffff;
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          height: 100vh;
          box-sizing: border-box;
        }
        .icon {
          width: 128px;
          height: 128px;
          margin-bottom: 20px;
          border-radius: 20px;
          box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
        }
        h1 {
          margin: 0 0 10px 0;
          font-size: 28px;
          font-weight: 600;
          color: #CC785C;
        }
        .tagline {
          margin: 0 0 30px 0;
          font-size: 16px;
          color: #999;
          text-align: center;
          max-width: 400px;
          line-height: 1.5;
        }
        .github-btn {
          display: inline-block;
          padding: 12px 30px;
          background: #CC785C;
          color: white;
          border: none;
          border-radius: 8px;
          font-size: 15px;
          font-weight: 600;
          cursor: pointer;
          text-decoration: none;
          transition: transform 0.2s, box-shadow 0.2s, background 0.2s;
          box-shadow: 0 4px 12px rgba(204, 120, 92, 0.3);
        }
        .github-btn:hover {
          transform: translateY(-2px);
          background: #D88770;
          box-shadow: 0 6px 20px rgba(204, 120, 92, 0.4);
        }
        .github-btn:active {
          transform: translateY(0);
        }
        .copyright {
          margin-top: 30px;
          font-size: 12px;
          color: #666;
          text-align: center;
        }
      </style>
    </head>
    <body>
      <img src="${iconDataUrl}" class="icon" />
      <h1>☁️ Cloude Code <span style="font-size: 16px; color: #666; font-weight: 400;">${appVersion}</span></h1>
      <p class="tagline">
        Your AI coding sidekick in the menu bar.<br/>
        Command Claude from anywhere, build anywhere.
      </p>
      <a class="github-btn" href="https://github.com/Adoom666/CloudeCode" target="_blank" rel="noopener noreferrer">View on GitHub</a>
      <div class="copyright">
        Copyright © ${currentYear} Psyance, LLC. All rights reserved.
      </div>
    </body>
    </html>
  `;

  aboutWindow.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
  aboutWindow.once('ready-to-show', () => {
    aboutWindow.show();
  });

  // Remove menu bar from the window
  aboutWindow.setMenu(null);
}

// Prevent app from quitting when all windows are closed (menu bar app behavior)
app.on('window-all-closed', () => {
  // Don't quit
});

app.whenReady().then(async () => {
  console.log('Cloude Code menu bar app starting...');

  // Initialize components
  serverManager = new ServerManager();
  launchAgentInstaller = new LaunchAgentInstaller();

  // Create tray icon FIRST so the user sees the app is alive even if first-run
  // provisioning takes 60-120s (pip install). No progress bars, no modals —
  // the tray tooltip is our only status surface.
  createTray();

  // First-run auto-provisioning. Packaged app bundles server resources under
  // app.getAppPath()/../ (i.e. <Contents>/Resources/). ServerManager already
  // stages them to Application Support/Cloude Code/server/ — but bootstrap
  // completes the picture by adding venv + .env + config.json + deps-hash.
  const serverDir = path.join(app.getPath('userData'), 'server');
  const bundleResourcesDir = app.isPackaged
    ? path.join(app.getAppPath(), '..')  // packaged: <Contents>/Resources/
    : path.join(__dirname, '..');         // dev: project root
  const bootstrapResult = await bootstrapIfNeeded({
    serverDir,
    bundleResourcesDir,
    isPackaged: app.isPackaged,
    appVersion: app.getVersion(),
    onStateChange: (state) => {
      const tooltip = BOOTSTRAP_TOOLTIPS[state] || `Cloude Code — ${state}`;
      if (tray) tray.setToolTip(tooltip);
      console.log('[bootstrap]', state);
    },
  });

  if (bootstrapResult.status === 'python-missing') {
    const { dialog, clipboard } = require('electron');
    const result = await dialog.showMessageBox({
      type: 'error',
      title: 'Python 3.12+ required',
      message: 'Cloude Code needs Python 3.12 or later.',
      detail: 'Install via Homebrew:\n\n  brew install python@3.12\n\nThen re-launch Cloude Code.',
      buttons: ['Copy command', 'Quit'],
      defaultId: 0,
    });
    if (result.response === 0) {
      clipboard.writeText('brew install python@3.12');
    }
    app.quit();
    return;
  }

  if (bootstrapResult.status === 'error') {
    const { dialog } = require('electron');
    dialog.showErrorBox(
      'Cloude Code setup failed',
      bootstrapResult.details || 'Unknown error during first-run provisioning. Check Console.app for [bootstrap] logs.'
    );
    app.quit();
    return;
  }

  // Start server automatically (ensureServerFiles + ensureVenv inside are
  // idempotent and will short-circuit since bootstrap already did the work).
  // start() throws on a real, user-actionable failure (config invalid, or the
  // port held by something that is not our server). Show it: an unhandled
  // rejection here would leave the app running with a menu bar that claims
  // nothing is wrong. Not fatal, so we do not quit — the user can free the
  // port and use Start Server.
  try {
    await serverManager.start();
  } catch (err) {
    await reportStartFailure(err);
  }

  // Force immediate health check to sync state before first menu update.
  //
  // GUARDED, and the guard is the point. This probe cannot tell OUR server
  // from a stranger on the same port: it asks "is something healthy on 8000",
  // gets yes, and promotes the state to 'running'. When start() has just
  // REFUSED to adopt that exact listener because it is a different version,
  // this line would quietly undo the refusal one statement later and the app
  // would carry on talking to the stale server anyway - the 2026-08-25 defect
  // surviving its own fix. If start() blocked, leave it blocked.
  const health = serverManager.startBlockedReason
    ? null
    : await serverManager.getHealth();
  if (health && serverManager.getState() !== 'running') {
    console.log('Initial health check succeeded, marking as running');
    serverManager.state = 'running';
    if (!serverManager.startTime) {
      serverManager.startTime = Date.now();
    }
  }

  // Update menu with correct state
  updateMenu();

  // Start polling for stats
  startStatsPolling();
  startTraySignalPolling();

  // Fresh install: auto-pop the TOTP QR so the user pairs their authenticator
  // before they ever need to log in. Fire-and-forget; server health poll has
  // a 30s ceiling.
  if (bootstrapResult.freshInstall) {
    console.log('[first-run] fresh install detected, awaiting server health before showing QR');
    waitForServerHealth().then((ok) => {
      if (ok) {
        showQrPairingWindow();
      } else {
        console.warn('[first-run] server did not become healthy in time; skipping auto-QR');
      }
    });
  }

  console.log('App ready!');
});

/**
 * Create the menu bar tray icon
 */
/**
 * Gather everything the tray status light is derived from.
 *
 * Kept as one function so the icon, the tooltip and the menu rows all read
 * from exactly the same snapshot. Deriving them separately is how a tooltip
 * ends up disagreeing with the icon beside it.
 *
 * @returns {object} Input shaped for tray-status.deriveTrayState.
 */
function currentTrayInput() {
  return {
    serverState: serverManager ? serverManager.getState() : 'stopped',
    lastExitUnexpected: Boolean(serverManager && serverManager.lastExitUnexpected),
    sessions: traySignals.sessions,
    sessionsReachable: traySignals.sessionsReachable,
    updateStatus: traySignals.updateStatus,
    // The setup verdict, which prefers the SERVER's answer from GET /health
    // and falls back to reading the same facts locally only when there is no
    // server to ask. The icon and the menu row must not be able to disagree,
    // so both go through getSetupVerdict() and neither reads
    // getSetupStatus() directly. tray-status treats null as unknown, never
    // as complete, so an unpolled instance cannot render as set up.
    setupStatus: serverManager ? serverManager.getSetupVerdict().status : null,
  };
}

/**
 * Apply the derived status light to the tray icon and tooltip.
 *
 * The healthy state loads the ORIGINAL asset as a real template image, so it
 * adapts to the menu bar exactly as it always has. Every other state needs a
 * coloured dot, and AppKit discards colour from a template image, so those
 * are non-template and come in a light and a dark variant chosen from
 * nativeTheme.
 *
 * If the state has not changed since the last call, the image is not
 * reloaded: setImage on every 5 second poll is wasted work and can make the
 * icon visibly flicker.
 *
 * @param {boolean} [force] - Reapply even when the state is unchanged, used
 *   when the system appearance flips and the same state needs a different
 *   variant.
 * @returns {void}
 */
function applyTrayStatus(force) {
  if (!tray) return;

  const input = currentTrayInput();
  const derived = trayStatus.deriveTrayState(input);

  tray.setToolTip(trayStatus.buildTooltip(input));

  if (!force && derived.state === currentTrayState) return;
  currentTrayState = derived.state;

  const asset = trayStatus.resolveIconAsset(
    derived.state,
    nativeTheme.shouldUseDarkColors,
    path.join(__dirname, 'assets')
  );

  const image = nativeImage.createFromPath(asset.path);
  if (image.isEmpty()) {
    // A missing generated asset must not leave the previous state's icon on
    // screen pretending to be current. Fall back to the base mark, which is
    // honest about being the plain glyph.
    console.warn('Tray asset missing, falling back to base mark:', asset.path);
    const base = nativeImage.createFromPath(
      path.join(__dirname, 'assets', 'iconTemplate.png')
    );
    base.setTemplateImage(true);
    tray.setImage(base);
    return;
  }

  image.setTemplateImage(asset.isTemplate);
  tray.setImage(image);
}

/**
 * Poll the authenticated endpoints that back the session and update signals.
 *
 * Failure is recorded as "could not determine" with a reason, never as an
 * empty session list. An empty list is a real measurement meaning the server
 * was asked and has nothing running; a failed poll is the absence of a
 * measurement, and rendering the two the same way is the false green this
 * whole feature exists to prevent.
 *
 * @returns {Promise<void>} Resolves once the snapshot has been refreshed.
 */
async function pollTraySignals() {
  if (!serverManager) return;

  if (serverManager.getState() !== 'running') {
    traySignals.sessions = null;
    traySignals.sessionsReachable = false;
    traySignals.sessionsError = 'server is not running';
    traySignals.updateReachable = false;
    traySignals.updateStatus = null;
    return;
  }

  const apiUrl = serverManager.getLocalApiUrl();
  if (!apiUrl) {
    traySignals.sessionsReachable = false;
    traySignals.sessionsError = 'server address could not be determined';
    return;
  }

  if (!trayApi) {
    trayApi = new TrayApiClient({
      baseUrl: apiUrl,
      getOtp: () => serverManager.getCurrentOtp(),
    });
  } else {
    trayApi.setBaseUrl(apiUrl);
  }

  const sessions = await trayApi.fetchSessions();
  traySignals.sessions = sessions.sessions;
  traySignals.sessionsReachable = sessions.reachable;
  traySignals.sessionsError = sessions.error;

  const update = await trayApi.fetchUpdateStatus();
  traySignals.updateReachable = update.reachable;
  traySignals.updateStatus = update.reachable ? update.status : null;
  traySignals.latestVersion = update.latestVersion;
}

function createTray() {
  // Try to load icon, fall back to default if not found
  let iconPath = path.join(__dirname, 'assets', 'iconTemplate.png');
  let icon;

  try {
    icon = nativeImage.createFromPath(iconPath);
    if (icon.isEmpty()) {
      console.warn('Icon file not found, using default');
      icon = nativeImage.createEmpty();
    }
    icon.setTemplateImage(true); // Make it adapt to dark/light mode
  } catch (err) {
    console.error('Error loading icon:', err);
    icon = nativeImage.createEmpty();
  }

  tray = new Tray(icon);
  tray.setToolTip('Cloude Code');

  // A non-template status icon carries its own colours, so the same state
  // needs a different file when the menu bar flips between light and dark.
  // Force a reapply on that event, since the STATE has not changed and the
  // normal short-circuit would skip it.
  nativeTheme.on('updated', () => applyTrayStatus(true));

  applyTrayStatus(true);

  // Build initial menu
  updateMenu();
}

/**
 * Build the "Bind IP" submenu + "Copy URL" menu items.
 *
 * Exposed as a helper so the menu-template array in updateMenu() stays
 * readable. Every call re-queries os.networkInterfaces() and the
 * serverManager's current bind setting — so the radio selection and URL
 * label stay in sync with the running server across polls.
 *
 * Radio semantics: Electron groups consecutive `type: 'radio'` items
 * within the same submenu into a single radio group automatically.
 * Don't insert non-radio items between them or the grouping breaks.
 */
function buildBindAndUrlItems() {
  const bindHost = serverManager.getBindHost();
  const localIps = serverManager.getLocalInterfaceIps();
  const publishedUrl = serverManager.getPublishedUrl();
  // The ROW describes what is in effect; it is not built from configuration.
  // getBindHost() above is used only to tick the right radio item, which is
  // a question about what was CHOSEN and is the one place configuration is
  // the correct source.
  const bindRow = trayStatus.describeBind({
    effectiveHost: serverManager.getEffectiveBindHost(),
    configuredHost: bindHost,
  });
  const measuredUrl = serverManager.getMeasuredUrl();

  // No certificate is passed because the server terminates plaintext HTTP
  // today. evaluateBinding sees an http scheme and returns insecure without
  // needing one; when TLS lands, the observed certificate goes here and the
  // same function starts doing name and expiry checks. Passing a fake
  // "secure" here would be the padlock-without-a-measurement this module
  // exists to refuse.
  const security = tlsStatus.evaluateBinding({ url: publishedUrl });

  const bindSubmenu = [
    {
      label: '127.0.0.1  (localhost only)',
      type: 'radio',
      checked: bindHost === '127.0.0.1',
      click: () => handleBindChange('127.0.0.1'),
    },
    ...localIps.map(({ iface, ip }) => ({
      // Name the Tailscale address for what it is. The interface is called
      // utun4, which tells the user nothing, and this is the address that
      // used to be missing from this menu entirely.
      label: isTailscaleIp(ip)
        ? `${ip}  (${iface}, Tailscale)`
        : `${ip}  (${iface})`,
      type: 'radio',
      checked: bindHost === ip,
      click: () => handleBindChange(ip),
    })),
    {
      label: '0.0.0.0  (all interfaces, LAN-exposed)',
      type: 'radio',
      checked: bindHost === '0.0.0.0',
      click: () => handleBindChange('0.0.0.0'),
    },
  ];

  return [
    {
      label: bindRow.label,
      toolTip: bindRow.detail,
      submenu: bindSubmenu,
    },
    {
      // Reports the CONNECTION, not the scheme. Today every binding is plain
      // HTTP so this always reads "not secure", which is the honest answer;
      // it becomes a real measurement the moment TLS is terminated, because
      // evaluateBinding checks the certificate's NAME before its expiry.
      label: security.label,
      enabled: false,
      toolTip: security.detail,
    },
    measuredUrl
      ? {
          label: `Copy URL: ${measuredUrl}`,
          click: () => {
            clipboard.writeText(measuredUrl);
            console.log(`[clipboard] wrote: ${measuredUrl}`);
          },
        }
      : {
          // Never hand him a URL assembled from configuration. He would
          // paste it somewhere and it would not answer, with nothing to say
          // why - the address looks exactly as legitimate as a real one.
          label: 'Copy URL: unavailable (bind not measured)',
          enabled: false,
          toolTip: bindRow.detail,
        },
  ];
}

/**
 * Handle a bind-host change from the submenu. Updates tooltip to
 * reflect in-flight restart, then refreshes the menu on completion.
 */
async function handleBindChange(ip) {
  if (tray) tray.setToolTip(`Cloude Code — restarting on ${ip}...`);
  try {
    await serverManager.setBindHost(ip);
  } catch (err) {
    console.error('[bind-host] change failed:', err);
    const { dialog } = require('electron');
    dialog.showErrorBox(
      'Bind IP change failed',
      `Could not restart the server on ${ip}.\n\n${err.message || err}\n\nFalling back to the previous binding.`
    );
  } finally {
    if (tray) tray.setToolTip('Cloude Code');
    updateMenu();
    // Health poll will auto-refresh menu within a few seconds; this just
    // gets the instant visual feedback right after the restart settles.
    setTimeout(updateMenu, 2500);
  }
}

/**
 * Label for the setup/config menu row, carrying an exclamation point when
 * something needs a human.
 *
 * Derives the marker from trayStatus.deriveTrayState over the SAME input the
 * icon uses, rather than from a second inspection of setup state. Two
 * independent notions of "needs attention" drift apart, and the one nobody
 * is looking at is the one that goes wrong.
 *
 * @returns {string} The menu item label.
 */
function setupMenuLabel() {
  const derived = trayStatus.deriveTrayState(currentTrayInput());
  // The VERDICT, which prefers the server's answer over any local reading.
  // Reading getSetupStatus() directly here would have been a fourth opinion.
  const status = serverManager ? serverManager.getSetupVerdict().status : null;
  // Only the setup-driven attention state earns the marker here. A session
  // waiting on a human is real, but it is not a reason to point him at the
  // configuration page.
  const needsSetup = status === 'incomplete' || status === 'undetermined';
  const marker = needsSetup && derived.state === 'attention' ? '(!)  ' : '';
  // The TEXT says what to do. "Setup and Config..." is a destination, and he
  // told us the previous warning was not self-explanatory.
  return `${marker}${trayStatus.describeSetupRow(status)}`;
}

/**
 * Open the setup/upgrade wizard in the user's default browser.
 *
 * Uses shell.openExternal for the same reason 'Open in Browser' does: this is
 * a menu-bar app with no window to host a page, and his own browser already
 * holds the session the wizard needs once setup is complete.
 *
 * The URL comes from getPublishedUrl(), which prefers the address the server
 * MEASURED itself onto. During the setup lockdown that is 127.0.0.1 while
 * configuration may say something else, and opening the configured address
 * would load nothing at all.
 *
 * @returns {void}
 */
function openSetupWizard() {
  const base = serverManager.getPublishedUrl();
  if (!base) {
    dialog.showErrorBox(
      'Cloude Code: unknown server port',
      'Could not determine the configured port from .env (PORT= is set but ' +
      'not a valid port number), so the setup page cannot be opened. Fix ' +
      '.env and restart Cloude Code.'
    );
    return;
  }
  if (serverManager.getState() !== 'running') {
    dialog.showMessageBox({
      type: 'info',
      title: 'Server not running',
      message: 'The setup page is served by the Cloude Code server.',
      detail: 'Start the server from this menu, then open Setup and Config again.',
      buttons: ['OK'],
    });
    return;
  }
  shell.openExternal(`${base}/setup`);
}

/**
 * Open the web app's settings screen.
 *
 * The URL comes from getPublishedUrl(), which prefers the address the
 * server MEASURED itself onto - during the setup lockdown that is
 * 127.0.0.1 while configuration may say something else, and opening the
 * configured address would load nothing at all. Same reasoning as
 * openSetupWizard().
 *
 * `#settings` is honoured by client/js/app.js once the launchpad is up,
 * so this works whether or not the browser already holds a session.
 *
 * @returns {void}
 */
function openWebSettings() {
  const base = serverManager.getPublishedUrl();
  if (!base) {
    dialog.showErrorBox(
      'Cloude Code: unknown server port',
      'Could not determine the configured port from .env (PORT= is set but ' +
      'not a valid port number), so the settings page cannot be opened. Fix ' +
      '.env and restart Cloude Code.'
    );
    return;
  }
  if (serverManager.getState() !== 'running') {
    dialog.showMessageBox({
      type: 'info',
      title: 'Server not running',
      message: 'The settings screen is served by the Cloude Code server.',
      detail: 'Start the server from this menu, then open Settings again.',
      buttons: ['OK'],
    });
    return;
  }
  shell.openExternal(`${base}/#settings`);
}

/**
 * Open a file in the user's configured editor.
 *
 * WHAT THIS REPLACES, AND WHY IT MATTERED. This menu item used to run
 * `open -R`, which REVEALS the file in Finder rather than opening it.
 * That is not merely a slower route to the same place: the running
 * server caches its parsed config in memory and only invalidates that
 * cache when the app itself writes the file, so an edit made through
 * Finder was invisible to the process it was meant to change. The user
 * saw his change on disk and no change in behaviour, with nothing
 * anywhere reporting a problem.
 *
 * Opening it in an editor does not fix the cache - a hand edit still
 * needs a restart - so the dialog says so rather than implying the
 * change took.
 *
 * Three outcomes: no editor configured falls back to the system default
 * (`shell.openPath`, which at least OPENS the file), a configured editor
 * that fails to launch reports the failure, and success says what to do
 * next.
 *
 * @param {string} filePath - absolute path to open.
 * @returns {void}
 */
function openInConfiguredEditor(filePath) {
  const fs = require('fs');
  const { exec } = require('child_process');

  let editor = null;
  try {
    const parsed = JSON.parse(fs.readFileSync(serverManager.getConfigPath(), 'utf8'));
    const configured = parsed && parsed.workspace && parsed.workspace.default_editor;
    if (typeof configured === 'string' && configured.trim()) editor = configured.trim();
  } catch (err) {
    // An unreadable config is exactly the case someone opens this item to
    // FIX, so it must not become a dead menu item. Fall through to the
    // system default.
    console.warn('[edit-config] could not read default_editor:', err.message);
  }

  const note = {
    type: 'info',
    title: 'Edit config',
    message: 'Restart the server after you save.',
    detail: 'The running server parsed this file at startup and caches it, ' +
            'so a hand edit does not take effect until it restarts. Changes ' +
            'made from the web app\'s settings screen apply without one.',
    buttons: ['OK'],
  };

  if (!editor) {
    shell.openPath(filePath).then((err) => {
      if (err) {
        dialog.showErrorBox('Cloude Code: could not open config',
          `macOS could not open ${filePath}: ${err}`);
        return;
      }
      dialog.showMessageBox(note);
    });
    return;
  }

  exec(`${editor} ${JSON.stringify(filePath)}`, (err) => {
    if (err) {
      dialog.showErrorBox(
        'Cloude Code: editor would not launch',
        `The configured editor could not open the file.\n\n` +
        `Command: ${editor}\n` +
        `File: ${filePath}\n\n` +
        `${err.message}\n\n` +
        'Change "default editor" on the settings screen, or clear it to use ' +
        'the system default.'
      );
      return;
    }
    dialog.showMessageBox(note);
  });
}

/**
 * Build and update the tray menu
 */
function updateMenu() {
  const state = serverManager.getState();
  const health = currentStats;

  // Read the signal verdicts from the SAME snapshot the icon is derived
  // from, so a menu row can never contradict the icon sitting above it.
  const traySignalLabels = trayStatus.describeSignals(currentTrayInput());

  const sessionName = health?.session_name || 'None';
  // health.tunnel_count has not existed since plan v3.2 demolished the
  // Cloudflare tunnel system, and it cannot come back by accident: FastAPI's
  // response_model is a FILTER, so any field HealthResponse does not declare
  // is deleted from the response before it is sent. This row therefore read
  // "Tunnels: 0" permanently, describing a subsystem the app does not have.
  // local_server_count is the field that replaced it, and src/models.py has
  // said this tray reads it for a while now - it did not, until 2026-08-26.
  // Three outcomes: a server that has not answered yet is not a server with
  // zero local servers, and the row says which it is.
  const localServerCount =
    typeof health?.local_server_count === 'number'
      ? String(health.local_server_count)
      : 'unknown';

  // Setup status, from the SERVER whenever it has answered. The tray used to
  // compute its own and got a different answer that the user could not act
  // on - see macOS/setup-verdict.js. Three outcomes: a server that could not
  // be asked is not an unconfigured instance and must not read as one.
  const setupVerdictNow = serverManager.getSetupVerdict();
  const configText =
    setupVerdictNow.status === 'complete'
      ? '✓ Setup: complete'
      : setupVerdictNow.status === 'incomplete'
        ? '⚠ Setup: not finished'
        : '⚠ Setup: unknown (could not be checked)';

  let statusText, statusIcon;
  switch (state) {
    case 'running':
      statusText = '● Server: Running';
      statusIcon = '●';
      break;
    case 'starting':
      statusText = '◐ Server: Starting...';
      statusIcon = '◐';
      break;
    case 'stopped':
    default:
      statusText = '○ Server: Stopped';
      statusIcon = '○';
      break;
  }

  const isRunning = state === 'running';
  const isStartingOrRunning = state === 'starting' || state === 'running';
  const canStart = state === 'stopped';
  const canStop = state === 'running' || state === 'starting';

  // Build menu items array
  const menuItems = [];

  // Offer the setup script ONLY on a DEFINITE incomplete. Not on
  // 'undetermined': sending somebody to re-run a setup that was never the
  // problem is the same defect as hiding one that was, pointed the other
  // way. This used to be gated on a private check that could never be
  // satisfied, so the row was permanent furniture on a finished install.
  if (setupVerdictNow.status === 'incomplete') {
    menuItems.push({
      label: '⚠️  Run Setup Script',
      click: () => {
        serverManager.openSetupScript();
      }
    });
  }

  // Status items (always shown)
  menuItems.push(
    {
      label: statusText,
      enabled: false
    },
    {
      label: `Session: ${sessionName}`,
      enabled: false
    },
    {
      label: `Local servers: ${localServerCount}`,
      enabled: false
    },
    {
      // Each signal states its OWN verdict, including "cannot determine".
      // The single tray icon has to prioritise one of them; these rows are
      // where nothing gets to hide behind that prioritisation.
      label: `Sessions: ${traySignalLabels.sessions}`,
      enabled: false
    },
    {
      label: `Update: ${traySignalLabels.update}`,
      enabled: false
    },
    { type: 'separator' },
    {
      label: 'Open Terminal Logs',
      click: () => {
        const fs = require('fs');
        const logPath = serverManager.logFile;

        // Check if log file exists (it only exists if app spawned the server)
        if (fs.existsSync(logPath)) {
          // Routes to the user's DEFAULT terminal via a .command document,
          // the same way Open in Browser routes to the default browser.
          // Falls back to Terminal.app (with an activate, so the window is
          // actually raised) and says so rather than silently substituting.
          const scriptPath = path.join(
            path.dirname(logPath),
            terminalLauncher.DEFAULT_SCRIPT_BASENAME
          );
          terminalLauncher.openServerLogInDefaultTerminal(
            logPath,
            scriptPath,
            (result) => {
              if (!result.opened) {
                dialog.showErrorBox(
                  'Cloude Code: could not open the log',
                  'The server log could not be opened in a terminal.\n\n' +
                    String(
                      (result.error && result.error.message) ||
                        result.error ||
                        ''
                    ) +
                    '\n\nThe log file itself is at:\n' +
                    logPath
                );
                return;
              }
              if (result.usedFallback) {
                dialog.showMessageBox({
                  type: 'info',
                  title: 'Opened in Terminal',
                  message: 'Opened the log in Terminal',
                  detail:
                    'No default handler for .command files could be ' +
                    'determined, so Terminal was used instead of your ' +
                    'default terminal app.',
                });
              }
            },
            { fs, shell, execFile: require('child_process').execFile }
          );
        } else {
          // Server was adopted, logs not captured by app
          const { dialog } = require('electron');
          dialog.showMessageBox({
            type: 'info',
            title: 'Logs Not Available',
            message: 'Server logs not available',
            detail: 'The server was already running when the app started, so logs were not captured. Restart the server from the app to enable log capture.'
          });
        }
      },
      enabled: isStartingOrRunning
    },
    {
      // feat/settings-gui - the global settings screen lives in the web
      // app (the user chose that over a native window), so this opens it
      // there rather than duplicating the controls in a tray submenu that
      // would then be a second place to look and a second place to be
      // wrong. Same shell.openExternal + published-URL reasoning as
      // 'Open in Browser' below.
      label: 'Settings...',
      click: () => openWebSettings(),
      enabled: isRunning
    },
    {
      label: 'Open in Browser',
      click: () => {
        // Use the published URL so remote LAN bindings work from the
        // user's browser — 'localhost' is dead when uvicorn binds to
        // a specific LAN interface. getPublishedUrl() returns null when
        // the configured port could not be determined (bad PORT= in
        // .env). Never open a guessed-port URL in that case.
        const url = serverManager.getPublishedUrl();
        if (!url) {
          dialog.showErrorBox(
            'Cloude Code: unknown server port',
            'Could not determine the configured port from .env (PORT= is set but not a valid port number). Fix .env and restart Cloude Code.'
          );
          return;
        }
        shell.openExternal(url);
      },
      enabled: isRunning
    },
    ...buildBindAndUrlItems(),
    { type: 'separator' },
    {
      label: 'Server',
      submenu: [
        // WHAT THE SUPERVISOR IS DOING, SAID OUT LOUD.
        //
        // An automatic restart nobody is told about is indistinguishable from
        // nothing happening, which is the masking failure mode that makes
        // restart loops a bad idea in the first place. A server that has been
        // given up on must say so plainly here, because the alternative - the
        // shipped behaviour - is a dead server the user has to guess about.
        ...buildSupervisorItems(),
        {
          label: 'Restart Server',
          click: async () => {
            // A REFUSAL MUST BE VISIBLE.
            //
            // This used to be a bare `await serverManager.restart()`. When the
            // server had been adopted rather than spawned, restart() refused
            // deep inside stop() and returned without a word, so clicking the
            // item did nothing observable and it was reported as broken. It
            // was not broken; it was mute.
            try {
              const result = await serverManager.restart();
              if (result && result.refused) {
                await offerTakeOwnership('restart', result);
              }
            } catch (err) {
              dialog.showErrorBox('Cloude Code could not restart the server', err.message);
            }
            updateMenu();
            setTimeout(updateMenu, 2500);
          },
          enabled: isRunning
        },
        {
          label: canStart ? 'Start Server' : 'Stop Server',
          click: async () => {
            try {
              if (canStart) {
                // An explicit Start is a fresh decision by the user. If the
                // supervisor had given up, having given up must not lock him
                // out of trying again.
                serverManager.resetSupervisor();
                await serverManager.start();
              } else {
                const result = await serverManager.stop();
                if (result && result.refused) {
                  await offerTakeOwnership('stop', result);
                }
              }
            } catch (err) {
              // The user explicitly asked for this, so they get an explicit
              // answer rather than a menu item that appears to do nothing.
              await reportStartFailure(err);
            }
            updateMenu();
            setTimeout(updateMenu, 500);
          },
          enabled: canStart || canStop
        },
        { type: 'separator' },
        {
          label: 'Launch at Login',
          type: 'checkbox',
          checked: launchAgentInstaller.isEnabled(),
          click: () => {
            const appPath = app.getPath('exe');
            launchAgentInstaller.toggle(appPath);
            setTimeout(updateMenu, 100);
          }
        },
        (() => {
          // "Copy OTP: 123456" — surfaces the live 6-digit code so users
          // can paste into the web client without digging out their phone.
          // Code is recomputed on every menu rebuild (5s health poll
          // cadence), so it stays fresh. Label shows "(rolls in Xs)" when
          // the window is within 5s of rollover — hints the user to wait.
          const otp = serverManager.getCurrentOtp();
          const remaining = serverManager.getOtpSecondsRemaining();
          const rollHint = (otp && remaining <= 5) ? `  (rolls in ${remaining}s)` : '';
          const label = otp ? `Copy OTP: ${otp}${rollHint}` : 'Copy OTP: (not configured)';
          return {
            label,
            enabled: !!otp,
            click: () => {
              const fresh = serverManager.getCurrentOtp();
              if (!fresh) return;
              clipboard.writeText(fresh);
              console.log(`[clipboard] wrote OTP: ${fresh}`);
              // Brief tooltip flash — no macOS notification permission
              // prompt, no modal. 2s matches typical copy-feedback patterns.
              if (tray) {
                tray.setToolTip('OTP copied to clipboard');
                setTimeout(() => { if (tray) tray.setToolTip('Cloude Code'); }, 2000);
              }
            }
          };
        })(),
        {
          label: 'Check for Updates...',
          // A NOTIFIER, NOT AN AUTO-UPDATER, and the distinction is a
          // certificate. Squirrel.Mac refuses an update it cannot
          // signature-validate, and this app is ad-hoc signed, so real
          // auto-update needs a paid Developer ID plus notarization -
          // and would then swap a bundle whose Python child owns a
          // schema-versioned database. This reads a version and tells
          // the user. Nothing is swapped, so nothing can be half-swapped.
          click: async () => {
            const { dialog, shell, app } = require('electron');
            const { checkForUpdate, RESULT_AVAILABLE, RESULT_CURRENT } =
              require('./update-check.js');
            const r = await checkForUpdate(app.getVersion());
            if (r.result === RESULT_AVAILABLE) {
              const choice = await dialog.showMessageBox({
                type: 'info',
                title: 'Update Available',
                message: `Cloude Code ${r.latest} is available.`,
                detail: `You are running ${r.current}.`,
                buttons: ['Open Release Page', 'Later'],
                defaultId: 0,
                cancelId: 1
              });
              if (choice.response === 0 && r.url) await shell.openExternal(r.url);
              return;
            }
            if (r.result === RESULT_CURRENT) {
              await dialog.showMessageBox({
                type: 'info',
                title: 'Up to Date',
                message: `Cloude Code ${r.current} is the latest release.`
              });
              return;
            }
            // THE THIRD OUTCOME REACHES THE USER. Rendering a failed
            // check as "up to date" would tell them something nobody
            // established - and they would act on it.
            await dialog.showMessageBox({
              type: 'warning',
              title: 'Could Not Check for Updates',
              message: 'The update check did not complete.',
              detail: `${r.detail || 'no reason reported'}\n\n` +
                      `You are running ${r.current}. This is not the same ` +
                      `as being up to date - it means the check failed.`
            });
          }
        },
        {
          label: 'Show QR for TOTP',
          // ONE OWNER. This menu item used to carry its own full copy of
          // the fetch-render-window logic, near-identical to
          // showQrPairingWindow(). Two copies meant every QR fix had to
          // be made twice, and the duplication was only noticed because
          // a search-and-replace refused to run against two matches
          // where it expected one.
          //
          // showQrPairingWindow() now generates the code locally as SVG
          // and shows its own error dialog, so this is a straight call.
          click: async () => {
            await showQrPairingWindow();
          }
        },
        {
          label: 'Edit Config',
          click: () => openInConfiguredEditor(
            path.join(serverManager.getProjectRoot(), 'config.json')
          )
        },
        {
          // Replaces the old 'Check Config for New Defaults...' dialog, which
          // shelled out to config_upgrade.py and dumped its stdout into an
          // alert reading "Some settings need your attention". That told him
          // which fields, and nothing about what any of them are, what his
          // value means, or what changes if he picks either side.
          //
          // The exclamation point is not a second notion of "needs work" -
          // it is the SAME tray signal machinery the icon above is derived
          // from (trayStatus.deriveTrayState), read from the same snapshot,
          // so the menu row can never contradict the icon.
          label: setupMenuLabel(),
          click: () => openSetupWizard()
        },
        { type: 'separator' },
        {
          label: 'Uninstall',
          submenu: [
            {
              label: '☢️  Nuke it from Orbit!',
              click: async () => {
                const { dialog } = require('electron');
                const { promptForNukeConfirmation } = require('./nuke-confirm');

                // We invoke nuke.sh with --skip-confirm below, which bypasses
                // the script's own typed-NUKE gate. That is deliberate (a
                // shell prompt nobody can see is a hang, not a gate), but it
                // means the ONLY confirmation is this one, so it has to carry
                // the same weight: the user types the word NUKE. A button
                // labelled "NUKE IT" is one mis-aimed click on an
                // irreversible action. See macOS/nuke-confirm.js.
                //
                // The old dialog here also still listed Cloudflare tunnel and
                // DNS deletion (demolished in plan v3.2) and never mentioned
                // the state directory or the refresh tokens it destroys.
                // No path is passed: the app does not resolve the state
                // directory itself, and inventing one here would restate a
                // fact that lives in src/config.py - exactly the duplication
                // that produced the "Cloude Code" vs "CloudeCode" defect.
                // nuke.sh resolves it, prints it, and refuses to run if it
                // cannot. The dialog says so rather than guessing.
                const confirmed = await promptForNukeConfirmation('');

                if (confirmed) {
                  console.log('Nuking system...');

                  // Stop server first
                  await serverManager.stop();

                  // Stop stats polling
                  if (statsUpdateInterval) {
                    clearTimeout(statsUpdateInterval);
                  }

                  // Run nuke.sh script
                  const { exec } = require('child_process');
                  const fs = require('fs');
                  const projectRoot = serverManager.getProjectRoot();
                  const nukeScript = path.join(projectRoot, 'nuke.sh');
                  const nukeLogFile = '/tmp/cloudecode-nuke.log';

                  // Create log stream
                  const logStream = fs.createWriteStream(nukeLogFile, { flags: 'a' });
                  const timestamp = new Date().toISOString();

                  logStream.write(`\n\n=== Nuke started at ${timestamp} ===\n`);
                  logStream.write(`Script path: ${nukeScript}\n`);
                  logStream.write(`Working directory: ${projectRoot}\n`);
                  logStream.write(`Command: "${nukeScript}" --skip-confirm\n\n`);

                  exec(`"${nukeScript}" --skip-confirm`, { cwd: projectRoot }, (error, stdout, stderr) => {
                    // Log all output
                    logStream.write(`STDOUT:\n${stdout}\n\n`);
                    if (stderr) {
                      logStream.write(`STDERR:\n${stderr}\n\n`);
                    }
                    logStream.write(`Exit code: ${error ? error.code : 0}\n`);

                    // Verify cleanup actually happened
                    const envPath = path.join(projectRoot, '.env');
                    const venvPath = path.join(projectRoot, 'venv');
                    const configPath = path.join(projectRoot, 'config.json');

                    const envExists = fs.existsSync(envPath);
                    const venvExists = fs.existsSync(venvPath);
                    const configExists = fs.existsSync(configPath);

                    logStream.write(`\nCleanup verification:\n`);
                    logStream.write(`  .env exists: ${envExists}\n`);
                    logStream.write(`  venv exists: ${venvExists}\n`);
                    logStream.write(`  config.json exists: ${configExists}\n`);

                    const cleanupSucceeded = !envExists && !venvExists;
                    logStream.write(`  Cleanup succeeded: ${cleanupSucceeded}\n`);
                    logStream.write(`=== Nuke finished at ${new Date().toISOString()} ===\n`);
                    logStream.end();

                    if (error) {
                      console.error('Nuke failed:', error);
                      dialog.showErrorBox(
                        'Nuke Failed',
                        `Failed to complete system reset:\n\n${error.message}\n\nCheck logs at: ${nukeLogFile}`
                      );
                    } else if (!cleanupSucceeded) {
                      // Script exited successfully but cleanup didn't happen
                      console.error('Nuke script succeeded but cleanup verification failed');
                      dialog.showErrorBox(
                        'Nuke Incomplete',
                        `Script completed but cleanup verification failed.\n\nFiles still exist:\n${envExists ? '  • .env\n' : ''}${venvExists ? '  • venv/\n' : ''}\n\nCheck logs at: ${nukeLogFile}`
                      );
                    } else {
                      console.log('Nuke completed successfully');
                      console.log('Nuke output:', stdout);
                      if (stderr) console.error('Nuke stderr:', stderr);

                      // Show success and quit
                      dialog.showMessageBox({
                        type: 'info',
                        title: 'System Reset Complete',
                        message: 'Cloude Code has been completely removed.',
                        detail: `All configuration has been deleted.\n\nLogs saved to: ${nukeLogFile}\n\nRun ./setup.sh to configure again.\n\nThe app will now quit.`,
                        buttons: ['OK']
                      }).then(() => {
                        app.quit();
                      });
                    }
                  });
                }
              }
            }
          ]
        }
      ]
    },
    { type: 'separator' },
    {
      label: 'About Cloude Code',
      click: () => {
        showAboutDialog();
      }
    },
    { type: 'separator' },
    {
      label: 'Quit Cloude Code',
      click: () => {
        // Just quit. The teardown belongs to the before-quit handler and
        // ONLY there.
        //
        // This used to clear the timers and await serverManager.stop() itself
        // before calling app.quit(), which meant the app had TWO quit paths
        // with different shutdown semantics: this one awaited the stop,
        // Cmd-Q's before-quit listener did not. Same build, same machine,
        // different outcome depending on how the user quit - which is most of
        // why "does quitting kill the server" measured both ways on
        // 2026-08-25. One path, one behaviour.
        console.log('Quit requested from the menu.');
        app.quit();
      }
    }
  );

  const menu = Menu.buildFromTemplate(menuItems);

  tray.setContextMenu(menu);

  // Cheap: applyTrayStatus reloads the image only when the state actually
  // changed, so calling it on every menu rebuild keeps icon and menu in
  // step without repainting on every poll.
  applyTrayStatus();
}

/**
 * Start polling server for stats updates
 */
/**
 * Poll the authenticated session and update signals on their own timer.
 *
 * Deliberately slower than the 5 second health poll. These are authenticated
 * round trips, and the server rate limits TOTP verification, so hammering
 * them buys nothing: a session needing attention is not a sub-minute
 * emergency. An immediate first run means the tray stops showing "not polled
 * yet" as soon as the server is up rather than waiting a full interval.
 *
 * @returns {void}
 */
function startTraySignalPolling() {
  const INTERVAL_MS = 20000;

  const tick = async () => {
    try {
      await pollTraySignals();
    } catch (error) {
      // A thrown poll must not leave the previous snapshot in place looking
      // current. Record it as could-not-determine with the reason.
      traySignals.sessionsReachable = false;
      traySignals.sessionsError = String((error && error.message) || error);
    }
    applyTrayStatus();
    updateMenu();
  };

  if (trayPollInterval) clearInterval(trayPollInterval);
  trayPollInterval = setInterval(tick, INTERVAL_MS);
  tick();
}

function startStatsPolling() {
  let pollInterval = 5000; // Default 5 seconds
  let fastPollCount = 0;
  const maxFastPolls = 12; // Poll fast for ~1 minute during startup

  const poll = async () => {
    const state = serverManager.getState();

    // Poll faster during startup
    if (state === 'starting' && fastPollCount < maxFastPolls) {
      pollInterval = 2000; // 2 seconds
      fastPollCount++;
    } else {
      pollInterval = 5000; // 5 seconds
      fastPollCount = 0;
    }

    // Check if server should be running (either we started it or adopted it)
    if (state === 'running' || state === 'starting' || serverManager.isProcessRunning()) {
      const health = await serverManager.getHealth();
      // THE ONLY PLACE AN ADOPTED SERVER'S DEATH CAN BE NOTICED.
      //
      // An adopted process emits no 'exit' event to this app - we never
      // spawned it, so there is no ChildProcess to listen to. Without this
      // call its death is invisible and the server stays down until the user
      // works out that the app has gone quiet, which is exactly what happened
      // on 2026-08-25. It also clears the restart budget once the server has
      // been continuously up for the healthy window.
      serverManager.superviseTick(Boolean(health));
      if (health) {
        currentStats = health;
        // If we got health response, mark as running
        if (state !== 'running') {
          console.log('Health check succeeded, marking as running');
          serverManager.state = 'running';
          if (!serverManager.startTime) {
            serverManager.startTime = Date.now();
          }
        }
        updateMenu();
      } else {
        // Server process running but API not responding - mark as "starting"
        if (serverManager.isProcessRunning()) {
          if (state !== 'starting') {
            console.log('Process running but health check failed, marking as starting');
            serverManager.state = 'starting';
          }
        } else {
          // No process and no health - mark as stopped
          if (state !== 'stopped') {
            console.log('No process detected and health check failed, marking as stopped');
            serverManager.state = 'stopped';
            serverManager.startTime = null;
          }
        }
        currentStats = null;
        updateMenu();
      }
    } else {
      currentStats = null;
      if (state !== 'stopped') {
        serverManager.state = 'stopped';
        serverManager.startTime = null;
      }
      updateMenu();
    }

    // Schedule next poll with dynamic interval
    if (statsUpdateInterval) {
      clearTimeout(statsUpdateInterval);
    }
    statsUpdateInterval = setTimeout(poll, pollInterval);
  };

  // Start polling immediately - status will naturally transition from
  // "Stopped" -> "Starting" -> "Running" based on health checks
  poll();
}

/**
 * Handle app quit
 */
/**
 * Menu rows describing the auto-restart supervisor, when it has anything to
 * say.
 *
 * Returns an EMPTY array in the ordinary case - a healthy server that has
 * never needed a restart does not need a row about it, and a row that is
 * always present and always says "fine" is furniture nobody reads. It appears
 * exactly when something happened.
 *
 * @returns {Array<object>} Menu item templates, possibly empty.
 */
function buildSupervisorItems() {
  if (!serverManager) return [];
  const status = serverManager.getSupervisorStatus();
  if (!status.gaveUp && !status.pending && status.attempts === 0) return [];

  const label = status.gaveUp
    ? `Automatic restart gave up after ${status.maxAttempts} attempts`
    : status.pending
      ? `Restarting automatically (attempt ${status.attempts} of ${status.maxAttempts})...`
      : `Restarted automatically (attempt ${status.attempts} of ${status.maxAttempts})`;

  return [
    { label, enabled: false },
    {
      label: 'Why?',
      click: () => {
        dialog.showMessageBox({
          type: status.gaveUp ? 'warning' : 'info',
          title: 'Automatic server restart',
          message: label,
          detail: status.message,
          buttons: ['OK'],
        });
      },
    },
    { type: 'separator' },
  ];
}

/**
 * Show why a stop or restart was refused, and offer to take ownership.
 *
 * The refusal itself is correct - Cloude Code does not stop a server it did
 * not start. What was wrong was that it happened in silence. From the menu, a
 * correct refusal and a broken menu item look exactly the same, and the user
 * reasonably read it as the latter.
 *
 * The destructive option is never the default button. Taking ownership means
 * killing a process this app did not start, which is a decision that belongs
 * to a person and should not ride on a stray Return key.
 *
 * @param {'stop'|'restart'} action - What was refused.
 * @param {{reason: string, offerTakeOwnership: boolean, pid: number|null}}
 *   result - The structured refusal from serverManager.
 * @returns {Promise<void>} Resolves once the user has answered and any
 *   takeover they asked for has been attempted.
 */
async function offerTakeOwnership(action, result) {
  const title =
    action === 'restart'
      ? 'Cloude Code did not start this server'
      : 'Cloude Code did not start this server';

  if (!result.offerTakeOwnership) {
    dialog.showErrorBox(title, result.reason);
    return;
  }

  const verb = action === 'restart' ? 'Restart' : 'Stop';
  const answer = await dialog.showMessageBox({
    type: 'warning',
    title,
    message: title,
    detail:
      `${result.reason}\n\n` +
      (result.pid ? `The running server is PID ${result.pid}.\n\n` : '') +
      `${verb} it anyway and let Cloude Code take ownership? Claude sessions ` +
      'running in tmux keep running - tmux outlives the server - but anything ' +
      'connected through the web client will be disconnected.',
    buttons: ['Leave it alone', `${verb} it anyway`],
    defaultId: 0,
    cancelId: 0,
  });

  if (answer.response !== 1) {
    console.log(`User declined to take ownership for ${action}.`);
    return;
  }

  try {
    if (action === 'restart') {
      await serverManager.restart({ takeOwnership: true });
    } else {
      await serverManager.stop({ takeOwnership: true });
    }
  } catch (err) {
    dialog.showErrorBox(`Cloude Code could not ${action} the server`, err.message);
  }
  updateMenu();
}

/**
 * Show a start() failure, and offer a way out of the one that has a way out.
 *
 * ADOPTION_REFUSED is not a generic error. start() found a healthy Cloude Code
 * server on the port that it could not prove is running THIS bundle's code -
 * on 2026-08-25 that was a v1.0.2 server, orphaned onto launchd, which a
 * v1.0.3 bundle adopted and then ran for four hours. Refusing to adopt is
 * right. Refusing and then leaving the user at a dead end is not: the only
 * remaining move would be Activity Monitor, and an app that tells you
 * something is wrong and nothing about what to do reads as broken.
 *
 * So the refusal becomes a choice, and the app does not make it. Killing a
 * process this app did not start is exactly the kind of decision that needs a
 * person, which is why the default button is the harmless one.
 *
 * @param {Error} err - The error start() threw.
 * @returns {Promise<void>} Resolves once the user has answered, and once any
 *   replacement they asked for has been attempted.
 */
async function reportStartFailure(err) {
  if (!err || err.code !== 'ADOPTION_REFUSED') {
    dialog.showErrorBox('Cloude Code could not start the server', err.message);
    return;
  }

  const title =
    err.outcome === 'mismatch'
      ? 'A different version of the Cloude Code server is already running'
      : 'Cloude Code cannot identify the server already running';

  const answer = await dialog.showMessageBox({
    type: 'warning',
    title,
    message: title,
    detail:
      `${err.reason || err.message}\n\n` +
      'Replacing it will stop that server. Any Claude sessions it is running ' +
      'in tmux keep running - tmux outlives the server - but anything ' +
      'connected to it through the web client will be disconnected until the ' +
      'new server is up.',
    buttons: ['Leave it running', 'Replace it'],
    defaultId: 0,
    cancelId: 0,
  });

  if (answer.response !== 1) {
    console.log('User declined to replace the server on the port.');
    return;
  }

  try {
    await serverManager.takeOverPort();
  } catch (takeoverErr) {
    dialog.showErrorBox(
      'Cloude Code could not replace the running server',
      takeoverErr.message
    );
  }
  updateMenu();
}

//
// THE QUIT MUST WAIT FOR THE SERVER TO ACTUALLY DIE.
//
// This was registered as a bare `async () => { ... await serverManager.stop() }`.
// Electron does not await an async before-quit listener: it calls the
// function, receives a pending promise, discards it, and carries on quitting.
// So every line after the first `await` inside stop() raced the main
// process's own teardown, and on 2026-08-25 that race was observed going BOTH
// ways on one machine and one build. When the app won, the Python server was
// reparented to launchd (ppid 1), kept serving port 8000, and was ADOPTED
// four hours later by a newer version of the app.
//
// The sequencing now lives in macOS/quit-sequence.js, which defers the quit,
// awaits the teardown, and re-issues the quit exactly once. It is there
// rather than here because this file cannot be loaded outside a running
// Electron, so anything left inline can only be checked by grepping its
// text - see tests/test_quit_is_deterministic.node.mjs.
app.on(
  'before-quit',
  createQuitHandler({
    teardown: async () => {
      // FIRST, before anything is stopped. Otherwise the supervisor sees the
      // shutdown as an unexpected death and races the quit to spawn a
      // replacement - a brand new orphan created at the exact moment the app
      // is going away.
      if (serverManager) {
        serverManager.beginQuit();
      }
      if (statsUpdateInterval) {
        clearTimeout(statsUpdateInterval);
      }
      if (trayPollInterval) {
        clearTimeout(trayPollInterval);
      }
      if (serverManager) {
        await serverManager.stop();
      }
    },
    quit: () => app.quit(),
    log: (msg) => console.log(msg),
    onError: (msg, err) => console.error(msg, err && err.message),
  })
);

/**
 * Handle app activation (macOS specific)
 */
app.on('activate', () => {
  // On macOS, clicking dock icon should show menu
  if (tray) {
    tray.popUpContextMenu();
  }
});
