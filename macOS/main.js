const { app, Tray, Menu, shell, nativeImage, clipboard } = require('electron');
const path = require('path');
const ServerManager = require('./server-manager');
const LaunchAgentInstaller = require('./launchagent-installer');
const { bootstrapIfNeeded } = require('./bootstrap');

let tray = null;
let serverManager = null;
let launchAgentInstaller = null;
let statsUpdateInterval = null;
let currentStats = null;

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
async function showQrPairingWindow() {
  const axios = require('axios');
  const { BrowserWindow } = require('electron');
  // Probe the actual bound host — loopback is unreachable when uvicorn
  // is bound to a specific LAN IP.
  const url = `${serverManager.getLocalApiUrl()}/api/v1/auth/qr`;

  try {
    const response = await axios.get(url, { timeout: 5000 });
    const qrDataUrl = response.data && response.data.qr_image;
    if (!qrDataUrl || !qrDataUrl.startsWith('data:image/png;base64,')) {
      throw new Error('Server returned unexpected QR response shape');
    }
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
        .qr{width:320px;height:320px;background:#fff;border-radius:12px;padding:14px;
          box-shadow:0 8px 32px rgba(0,0,0,0.4);}
        .footer{margin-top:20px;font-size:11px;color:#666;}
      </style></head><body>
        <h1>☁️ Welcome to Cloude Code</h1>
        <p>Scan this QR with Google Authenticator, 1Password, Authy — any TOTP app.</p>
        <img src="${qrDataUrl}" class="qr" alt="TOTP QR code" />
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
  await serverManager.start();

  // Force immediate health check to sync state before first menu update
  const health = await serverManager.getHealth();
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

  const bindSubmenu = [
    {
      label: '127.0.0.1  (localhost only)',
      type: 'radio',
      checked: bindHost === '127.0.0.1',
      click: () => handleBindChange('127.0.0.1'),
    },
    ...localIps.map(({ iface, ip }) => ({
      label: `${ip}  (${iface})`,
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
      label: `Bind IP: ${bindHost}`,
      submenu: bindSubmenu,
    },
    {
      label: `Copy URL: ${publishedUrl}`,
      click: () => {
        clipboard.writeText(publishedUrl);
        console.log(`[clipboard] wrote: ${publishedUrl}`);
      },
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
 * Build and update the tray menu
 */
function updateMenu() {
  const state = serverManager.getState();
  const health = currentStats;

  const sessionName = health?.session_name || 'None';
  const tunnelCount = health?.tunnel_count || 0;

  // Check configuration status
  const configStatus = serverManager.checkConfiguration();
  const configText = configStatus.isConfigured
    ? '✓ Configuration: OK'
    : '⚠ Configuration: Setup Required';

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

  // Only show setup script option if config is not complete
  if (!configStatus.isConfigured) {
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
      label: `Tunnels: ${tunnelCount}`,
      enabled: false
    },
    { type: 'separator' },
    {
      label: 'Open Terminal Logs',
      click: () => {
        const { exec } = require('child_process');
        const fs = require('fs');
        const logPath = serverManager.logFile;

        // Check if log file exists (it only exists if app spawned the server)
        if (fs.existsSync(logPath)) {
          // Open Terminal and tail the server logs
          exec(`osascript -e 'tell application "Terminal" to do script "tail -f \\"${logPath}\\""'`);
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
      label: 'Open in Browser',
      click: () => {
        // Use the published URL so remote LAN bindings work from the
        // user's browser — 'localhost' is dead when uvicorn binds to
        // a specific LAN interface.
        shell.openExternal(serverManager.getPublishedUrl());
      },
      enabled: isRunning
    },
    ...buildBindAndUrlItems(),
    { type: 'separator' },
    {
      label: 'Server',
      submenu: [
        {
          label: 'Restart Server',
          click: async () => {
            await serverManager.restart();
            updateMenu();
            setTimeout(updateMenu, 2500);
          },
          enabled: isRunning
        },
        {
          label: canStart ? 'Start Server' : 'Stop Server',
          click: async () => {
            if (canStart) {
              await serverManager.start();
            } else {
              await serverManager.stop();
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
          label: 'Show QR for TOTP',
          click: async () => {
            // Fetch the QR image live from the running server so it ALWAYS
            // matches the .env the server was started with — no more stale
            // on-disk copies out of sync with the active secret.
            const axios = require('axios');
            const { BrowserWindow, dialog } = require('electron');
            // Probe the actual bound host — hardcoded 127.0.0.1 fails
            // when uvicorn binds exclusively to a LAN interface.
            const url = `${serverManager.getLocalApiUrl()}/api/v1/auth/qr`;

            try {
              const response = await axios.get(url, {
                timeout: 5000
              });

              const qrDataUrl = response.data && response.data.qr_image;
              if (!qrDataUrl || !qrDataUrl.startsWith('data:image/png;base64,')) {
                throw new Error('Server returned unexpected QR response shape');
              }

              const qrWindow = new BrowserWindow({
                width: 420,
                height: 520,
                resizable: false,
                minimizable: false,
                maximizable: false,
                fullscreenable: false,
                show: false,
                backgroundColor: '#1a1a1a',
                title: 'Cloude Code — TOTP QR',
                webPreferences: {
                  nodeIntegration: false,
                  contextIsolation: true,
                  sandbox: true
                }
              });

              const html = `
                <!DOCTYPE html>
                <html>
                <head>
                  <meta charset="utf-8">
                  <style>
                    body {
                      margin: 0;
                      padding: 32px;
                      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                      background: linear-gradient(135deg, #1a1a1a 0%, #2d2d2d 100%);
                      color: #ffffff;
                      display: flex;
                      flex-direction: column;
                      align-items: center;
                      justify-content: center;
                      height: 100vh;
                      box-sizing: border-box;
                    }
                    h1 {
                      margin: 0 0 8px 0;
                      font-size: 22px;
                      font-weight: 600;
                      color: #CC785C;
                    }
                    p {
                      margin: 0 0 20px 0;
                      font-size: 13px;
                      color: #999;
                      text-align: center;
                      max-width: 340px;
                      line-height: 1.5;
                    }
                    .qr {
                      width: 320px;
                      height: 320px;
                      background: #ffffff;
                      border-radius: 12px;
                      padding: 14px;
                      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
                    }
                    .footer {
                      margin-top: 20px;
                      font-size: 11px;
                      color: #666;
                    }
                  </style>
                </head>
                <body>
                  <h1>☁️ Scan with your authenticator</h1>
                  <p>Google Authenticator, 1Password, Authy — any TOTP app works.</p>
                  <img src="${qrDataUrl}" class="qr" alt="TOTP QR code" />
                  <div class="footer">Already set up? You can close this window.</div>
                </body>
                </html>
              `;

              qrWindow.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
              qrWindow.once('ready-to-show', () => {
                qrWindow.show();
              });
              qrWindow.setMenu(null);
            } catch (err) {
              const isConnErr = err.code === 'ECONNREFUSED' ||
                                err.code === 'ETIMEDOUT' ||
                                err.code === 'ECONNABORTED';
              dialog.showMessageBox({
                type: 'error',
                title: 'QR Code Unavailable',
                message: isConnErr
                  ? 'The Cloude Code server isn\'t running.'
                  : 'Could not fetch TOTP QR code',
                detail: isConnErr
                  ? 'Start the server from the menu, then try again.'
                  : `GET ${url} failed: ${err.message}`
              });
            }
          }
        },
        {
          label: 'Edit Config',
          click: () => {
            const { exec } = require('child_process');
            const configPath = path.join(serverManager.getProjectRoot(), 'config.json');
            // Open Finder and select the config.json file
            exec(`open -R "${configPath}"`);
          }
        },
        { type: 'separator' },
        {
          label: 'Uninstall',
          submenu: [
            {
              label: '☢️  Nuke it from Orbit!',
              click: async () => {
                const { dialog } = require('electron');

                // Show confirmation dialog
                const result = await dialog.showMessageBox({
                  type: 'warning',
                  title: 'Nuke it from Orbit!',
                  message: 'Complete System Reset',
                  detail:
                    'This will completely remove ALL Cloude Code configuration:\n\n' +
                    '✗ Cloudflare tunnel will be DELETED\n' +
                    '✗ All DNS records will be DELETED\n' +
                    '✗ All local configuration files\n' +
                    '✗ Python virtual environment\n' +
                    '✗ All logs and temporary files\n' +
                    '✗ Cloudflared authentication\n' +
                    '✗ macOS app settings\n\n' +
                    'You will need to run setup.sh again to use Cloude Code.\n\n' +
                    'Are you ABSOLUTELY SURE?',
                  buttons: ['Cancel', 'NUKE IT'],
                  defaultId: 0,
                  cancelId: 0
                });

                if (result.response === 1) {
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
      click: async () => {
        console.log('Quitting app...');

        // Stop stats polling
        if (statsUpdateInterval) {
          clearTimeout(statsUpdateInterval);
        }

        // Stop server
        await serverManager.stop();

        // Quit app
        app.quit();
      }
    }
  );

  const menu = Menu.buildFromTemplate(menuItems);

  tray.setContextMenu(menu);
}

/**
 * Start polling server for stats updates
 */
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
app.on('before-quit', async () => {
  console.log('App quitting...');

  if (statsUpdateInterval) {
    clearTimeout(statsUpdateInterval);
  }

  if (serverManager) {
    await serverManager.stop();
  }
});

/**
 * Handle app activation (macOS specific)
 */
app.on('activate', () => {
  // On macOS, clicking dock icon should show menu
  if (tray) {
    tray.popUpContextMenu();
  }
});
