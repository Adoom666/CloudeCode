const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');
const os = require('os');

class LaunchAgentInstaller {
  constructor() {
    this.launchAgentDir = path.join(os.homedir(), 'Library', 'LaunchAgents');
    this.plistName = 'com.cloudecode.menubar.plist';
    this.plistPath = path.join(this.launchAgentDir, this.plistName);
  }

  /**
   * Check if auto-launch is currently enabled
   * @returns {boolean}
   */
  isEnabled() {
    return fs.existsSync(this.plistPath);
  }

  /**
   * Enable auto-launch on login
   * @param {string} appPath - Path to the .app bundle
   */
  enable(appPath) {
    console.log('Enabling auto-launch...');
    console.log(`App path: ${appPath}`);

    // Ensure LaunchAgents directory exists
    if (!fs.existsSync(this.launchAgentDir)) {
      fs.mkdirSync(this.launchAgentDir, { recursive: true });
    }

    // Create plist content
    const plistContent = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.cloudecode.menubar</string>
    <key>ProgramArguments</key>
    <array>
        <string>${appPath}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/cloudecode-menubar.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/cloudecode-menubar-error.log</string>
</dict>
</plist>`;

    // Write plist file
    fs.writeFileSync(this.plistPath, plistContent, 'utf8');
    console.log(`Created LaunchAgent plist at: ${this.plistPath}`);

    // Load the launch agent
    try {
      execSync(`launchctl load "${this.plistPath}"`, { stdio: 'inherit' });
      console.log('LaunchAgent loaded successfully');
    } catch (err) {
      console.error('Failed to load LaunchAgent:', err.message);
    }
  }

  /**
   * Disable auto-launch on login
   */
  disable() {
    console.log('Disabling auto-launch...');

    if (!fs.existsSync(this.plistPath)) {
      console.log('LaunchAgent plist not found, nothing to disable');
      return;
    }

    // Unload the launch agent
    try {
      execSync(`launchctl unload "${this.plistPath}"`, { stdio: 'inherit' });
      console.log('LaunchAgent unloaded successfully');
    } catch (err) {
      console.error('Failed to unload LaunchAgent:', err.message);
    }

    // Remove plist file
    try {
      fs.unlinkSync(this.plistPath);
      console.log('LaunchAgent plist removed');
    } catch (err) {
      console.error('Failed to remove plist:', err.message);
    }
  }

  /**
   * Toggle auto-launch on/off
   * @param {string} appPath - Path to the .app bundle
   */
  toggle(appPath) {
    if (this.isEnabled()) {
      this.disable();
    } else {
      this.enable(appPath);
    }
  }
}

module.exports = LaunchAgentInstaller;
