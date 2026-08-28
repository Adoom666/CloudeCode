'use strict';

/**
 * Typed-NUKE confirmation gate for the macOS app.
 *
 * WHY THIS EXISTS
 * ---------------
 * The tray menu runs `nuke.sh --skip-confirm`, which bypasses the script's
 * own `read -p "type NUKE to confirm"` gate. That is the correct call for a
 * GUI - a shell prompt on a stream nobody is watching is not a gate, it is a
 * hang - but it moves the entire burden of confirmation onto the Electron
 * side. A message box with a "NUKE IT" button is not equivalent weight: it is
 * one mis-aimed click, from a menu that also holds ordinary items, on an
 * action that deletes the session database and the stored refresh tokens and
 * cannot be undone.
 *
 * So the GUI reproduces the shell gate exactly: the user must TYPE the word
 * NUKE. The confirm control stays disabled until the field matches, Escape
 * cancels, and the default focus is not on the destructive control.
 *
 * SANDBOX-SAFE CHANNEL
 * --------------------
 * The window runs with `nodeIntegration: false`, `contextIsolation: true`,
 * `sandbox: true` - the same secure defaults as showAboutDialog(). With no
 * preload there is no ipcRenderer, so the page reports its verdict by setting
 * `document.title`, which the main process reads through the
 * `page-title-updated` event. One-way, no privileged API exposed to a page
 * whose only job is to collect five characters.
 */

const RESULT_CONFIRMED = 'cloude-nuke:confirmed';
const RESULT_CANCELLED = 'cloude-nuke:cancelled';

/**
 * Description: build the confirmation page. Kept as a function so the HTML is
 *   testable without launching Electron.
 * Inputs: stateDirLabel (string) - the state directory path to name in the
 *   warning, or an empty string when it could not be resolved.
 * Output: string - a complete HTML document.
 */
function buildConfirmHtml(stateDirLabel) {
  const stateLine = stateDirLabel
    ? `<li>The state directory, <b>including your stored login refresh tokens</b>:<br><code>${escapeHtml(
        stateDirLabel
      )}</code></li>`
    : `<li>The state directory, <b>including your stored login refresh tokens</b>
         (path could not be determined here; nuke.sh resolves it itself and
         refuses to run if it cannot)</li>`;

  return `<!DOCTYPE html>
<html>
<head><meta charset="utf-8">
<style>
  body { margin:0; padding:28px; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         background:#1a1a1a; color:#eee; font-size:13px; }
  h1 { font-size:17px; margin:0 0 12px; color:#ff6b6b; }
  ul { margin:0 0 16px 18px; padding:0; line-height:1.55; }
  code { background:#000; padding:1px 4px; border-radius:3px; font-size:11px; word-break:break-all; }
  p.ask { margin:16px 0 8px; }
  input { width:100%; box-sizing:border-box; padding:9px 10px; font-size:15px; font-family:monospace;
          letter-spacing:2px; background:#000; color:#fff; border:1px solid #555; border-radius:5px; }
  input:focus { outline:none; border-color:#ff6b6b; }
  .row { display:flex; gap:10px; justify-content:flex-end; margin-top:18px; }
  button { padding:8px 18px; font-size:13px; border-radius:6px; border:1px solid #555;
           background:#2d2d2d; color:#eee; cursor:pointer; }
  button#go { background:#7a1f1f; border-color:#a33; }
  button#go:disabled { background:#2d2d2d; border-color:#444; color:#666; cursor:not-allowed; }
</style></head>
<body>
  <h1>Complete System Reset</h1>
  <p>This permanently removes, with no undo:</p>
  <ul>
    <li>All local configuration (<code>.env</code>, <code>config.json</code>)</li>
    <li>The Python virtual environment</li>
    ${stateLine}
    <li>The session database and migration trail</li>
    <li>All logs, temporary files, macOS app settings and the LaunchAgent</li>
  </ul>
  <p class="ask">Type <b>NUKE</b> to confirm:</p>
  <input id="word" autocomplete="off" autocorrect="off" spellcheck="false">
  <div class="row">
    <button id="cancel">Cancel</button>
    <button id="go" disabled>Nuke it from Orbit</button>
  </div>
<script>
  var input = document.getElementById('word');
  var go = document.getElementById('go');
  var cancel = document.getElementById('cancel');
  function cancelled() { document.title = ${JSON.stringify(RESULT_CANCELLED)}; }
  input.addEventListener('input', function () { go.disabled = input.value !== 'NUKE'; });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && input.value === 'NUKE') { document.title = ${JSON.stringify(
      RESULT_CONFIRMED
    )}; }
    if (e.key === 'Escape') { cancelled(); }
  });
  go.addEventListener('click', function () {
    if (input.value === 'NUKE') { document.title = ${JSON.stringify(RESULT_CONFIRMED)}; }
  });
  cancel.addEventListener('click', cancelled);
  input.focus();
</script>
</body></html>`;
}

/**
 * Description: minimal HTML escape for the one interpolated value.
 * Inputs: value (string).
 * Output: string.
 */
function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/**
 * Description: open the modal gate and resolve with the user's verdict.
 *   Closing the window by any other means (red button, app quit) resolves
 *   false - an unanswered gate is never a yes.
 * Inputs: stateDirLabel (string) - path to name in the warning; may be ''.
 * Output: Promise<boolean> - true only if the user typed NUKE and confirmed.
 */
function promptForNukeConfirmation(stateDirLabel) {
  const { BrowserWindow } = require('electron');
  return new Promise((resolve) => {
    const win = new BrowserWindow({
      width: 520,
      height: 470,
      resizable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      show: false,
      backgroundColor: '#1a1a1a',
      title: 'Complete System Reset',
      webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true }
    });

    let settled = false;
    const finish = (verdict) => {
      if (settled) return;
      settled = true;
      resolve(verdict);
      if (!win.isDestroyed()) win.close();
    };

    win.webContents.on('page-title-updated', (event, title) => {
      event.preventDefault();
      if (title === RESULT_CONFIRMED) finish(true);
      else if (title === RESULT_CANCELLED) finish(false);
    });
    // A window dismissed any other way is a NO, never a yes.
    win.on('closed', () => finish(false));

    win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    win.loadURL(
      'data:text/html;charset=utf-8,' +
        encodeURIComponent(buildConfirmHtml(stateDirLabel))
    );
    win.once('ready-to-show', () => win.show());
  });
}

module.exports = {
  promptForNukeConfirmation,
  buildConfirmHtml,
  RESULT_CONFIRMED,
  RESULT_CANCELLED
};
