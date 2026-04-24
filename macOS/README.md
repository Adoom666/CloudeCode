# Cloude Code - macOS Menu Bar App

Native macOS menu bar application for managing Cloude Code server.

## Quick Start

```bash
# Install dependencies
npm install

# Optional: Generate placeholder icons
cd assets && ./generate-icons.sh && cd ..

# Run in development mode
npm start

# Build DMG installer
npm run build
```

## Architecture

```
┌─────────────────────────────┐
│  Electron Main Process      │
│  (main.js)                  │
│    ├── Tray Manager         │
│    ├── Menu Builder         │
│    └── Stats Polling (5s)   │
└────────┬────────────────────┘
         │
    ┌────┴────────────────┐
    │                     │
┌───▼──────────┐  ┌──────▼─────────────┐
│ Server       │  │ LaunchAgent        │
│ Manager      │  │ Installer          │
│              │  │                    │
│ • Start      │  │ • Install plist    │
│ • Stop       │  │ • Remove plist     │
│ • Restart    │  │ • Toggle auto-     │
│ • Health     │  │   launch           │
│   Check      │  │                    │
└───┬──────────┘  └────────────────────┘
    │
    │ spawns & controls
    ▼
┌────────────────────────────────┐
│  Python FastAPI Server         │
│  (../venv/bin/python3)         │
│                                │
│  API: http://localhost:8000    │
│  • GET /api/v1/health          │
│  • POST /api/v1/shutdown       │
│  • Web UI accessible           │
└────────────────────────────────┘
```

## Files

- **main.js** - Electron main process, tray menu, stats polling
- **server-manager.js** - Python subprocess lifecycle management
- **launchagent-installer.js** - macOS auto-launch setup
- **preload.js** - IPC security bridge (minimal, for future use)
- **package.json** - NPM config with electron-builder setup
- **assets/** - Menu bar icons (22x22, 44x44)

## Menu Structure

```
● Server: Running
Session: my-project
Tunnels: 2
────────────────
Open Terminal
Open in Browser
────────────────
Restart Server
Stop Server
────────────────
Launch at Login ✓
Quit Cloude Code
```

## How It Works

### Server Management

1. **Start**: Spawns `../venv/bin/python3 -m src.main` subprocess
2. **Monitor**: Polls `GET /api/v1/health` every 5 seconds
3. **Restart**: Calls `POST /api/v1/shutdown`, waits 2s, then starts
4. **Stop**: Sends shutdown API call + SIGTERM signal

### Stats Polling

```javascript
setInterval(async () => {
  const stats = await fetch('http://localhost:8000/api/v1/health');
  // Update menu with: status, session_name, tunnel_count
}, 5000);
```

### Auto-Launch

Creates `~/Library/LaunchAgents/com.cloudecode.menubar.plist`:

```xml
<plist>
  <dict>
    <key>Label</key>
    <string>com.cloudecode.menubar</string>
    <key>ProgramArguments</key>
    <array>
      <string>/Applications/Cloude Code.app/Contents/MacOS/Cloude Code</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
  </dict>
</plist>
```

## Building

```bash
npm run build
```

Creates:
- `dist/Cloude Code.dmg` - macOS installer
- `dist/mac/Cloude Code.app` - App bundle

## Distribution

The build bundles:
- Electron runtime
- All JavaScript files
- Python venv, src/, client/ from parent directory
- .env, config.json

**Bundle size**: ~200-300MB (includes entire Python venv)

### Optimization (Future)

Use PyInstaller to create standalone Python binary:

```bash
cd ..
pyinstaller --onefile --add-data "src:src" --add-data "client:client" src/main.py
# Then bundle dist/main instead of venv/ (~50MB vs ~300MB)
```

## Development

### Running Locally

```bash
npm start
```

**Note**: Electron app will appear in menu bar, not Dock. Look for icon in top-right.

### Debugging

Server logs appear in Electron console:
- stdout: `[SERVER] ...`
- stderr: `[SERVER ERROR] ...`

View with: `npm start` (logs to terminal)

### Testing Without GUI

```bash
# Syntax check
node -c main.js

# Test server manager module
node -e "const SM = require('./server-manager'); const sm = new SM(); console.log('OK')"
```

## Icon Guidelines

See `assets/README.md` for:
- Design requirements
- Size specifications
- Generation scripts
- .icns conversion

## Troubleshooting

### App doesn't appear in menu bar

- Check Electron version compatibility
- Try: System Preferences → Security → Allow "Cloude Code"
- Check console for errors

### Server won't start

- Verify Python venv exists at `../venv`
- Check `which python3` points to venv
- Test manually: `cd .. && source venv/bin/activate && python3 -m src.main`

### Health endpoint fails

- Ensure server is running on port 8000
- Check firewall isn't blocking localhost
- Verify `/api/v1/health` endpoint exists (see src/api/routes.py)

### Auto-launch not working

- Check plist exists: `ls ~/Library/LaunchAgents/com.cloudecode.menubar.plist`
- Validate plist: `plutil ~/Library/LaunchAgents/com.cloudecode.menubar.plist`
- Check launchctl: `launchctl list | grep cloudecode`

## License

MIT (same as parent project)
