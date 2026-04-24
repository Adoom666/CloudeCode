# Cloude Code

Remote-control UI for Claude Code CLI sessions on your Mac. Terminal lives in tmux,
reachable from the browser on your phone, laptop, or any LAN-connected device.

[![Cloude Code Demo](https://img.youtube.com/vi/tGcRtH_RLiE/0.jpg)](https://www.youtube.com/shorts/tGcRtH_RLiE)

> **Quick Demo:** Watch Cloude Code in action — launchpad, adopt-external, real-time
> terminal streaming to a phone browser.

---

## Download

**macOS (Apple Silicon):** [Cloude.Code-0.5.0-arm64.dmg](https://github.com/Adoom666/CloudeCode/releases/download/v0.5.1/Cloude.Code-0.5.0-arm64.dmg) (93 MB)

> Note: the release tag is **v0.5.1** but the DMG filename still reads `0.5.0`. The internal electron-builder version will be bumped in the next release; the file you're downloading is the v0.5.1 build.

Drag the app into Applications, double-click. First launch auto-provisions a Python venv, installs dependencies, generates TOTP + JWT secrets, and pops a QR for you to scan with any authenticator app. Requires Python 3.12+ (install via `brew install python@3.12` if missing — the app detects and guides you).

**Verify the download** (optional):

```bash
shasum -a 256 Cloude.Code-0.5.0-arm64.dmg
# expected: 9bca6c6509891a70cfefeb2f8cae95c78613f5a90057f28cba25c30ad400d0be
```

**Other versions:** see [Releases](https://github.com/Adoom666/CloudeCode/releases).

---

## Overview

Cloude Code is a hybrid Electron + FastAPI + tmux control plane for Claude Code CLI
sessions. An Electron menu-bar app on your Mac spawns a Python FastAPI server.
The server talks to a dedicated tmux socket (`tmux -L cloude`) where every Claude
session lives as a detached pane. A web UI connects over WebSocket, streams the
pane bytes, and gives you a launchpad to start, adopt, detach, or kill sessions.

The whole thing is built for one scenario: **you're on the couch, the server's
at the desk, and you want to keep working on the project you left running.**
Authentication is TOTP (any RFC 6238 app — Google Authenticator, Authy, 1Password)
plus a 15-minute JWT access token + 7-day refresh token with reuse detection.
WebSocket auth rides on the `Sec-WebSocket-Protocol` subprotocol header so the
JWT never lands in a query string or a proxy access log.

**Threat model is LAN-only.** The intended exposure path is UniFi Teleport,
Tailscale, or a similar identity-aware overlay network. Cloude Code ships with
hardened defaults (strict CSP, rate-limited auth, JWT typ enforcement, owned-set
ACL for adopt), but this is **not** designed to stand naked on the public
internet. The optional Cloudflare-tunnel backend exists for convenience, not
because the app has been hardened for hostile traffic.

---

## Features

- **TOTP + JWT auth** — 6-digit TOTP unlocks a 15m access token + 7d refresh
  token; refresh rotates with reuse-detection that revokes the entire chain on
  a replayed refresh.
- **WebSocket subprotocol auth** — JWT flows through `Sec-WebSocket-Protocol`,
  not a query string. Close codes 4401 (auth) / 4400 (malformed) per RFC 6455.
- **Tmux persistence** — sessions live on a dedicated socket. Restart the server,
  reboot, nuke the Electron app — tmux keeps your pane alive and the launchpad
  re-adopts it on next boot.
- **Verbatim session naming** — project "Cloude Code Dev" becomes tmux session
  `cloude_Cloude Code Dev`. The only transforms: `.`→`_`, `:`→`_`, collapse
  whitespace runs, strip edges. Legacy `cloude_ses_<hex>` sessions stay
  supported.
- **Adopt-external sessions** — start a tmux session by hand
  (`tmux -L cloude new -s mywork`) and the launchpad lists it for one-click
  adoption. Cloude-owned vs user-owned is cross-referenced against a persisted
  owned-set, not a spoofable prefix.
- **Detach-not-destroy invariant** — switching sessions never kills. The X
  button is the *only* kill path in the UI. `tmux kill-session` is the only
  kill path in the shell. Everything else detaches.
- **Dynamic resize** — a WS resize handshake on every connect: server requests
  dims, client measures and replies, backend resizes the tmux window, Ctrl+L
  forces a clean redraw. No scrollback replay at the wrong geometry.
- **Deep-link routing** — `/session/<project>` serves the SPA shell; the
  client-side router auto-selects the project post-auth.
- **ntfy push notifications** — opt-in. IdleWatcher FSM detects permission
  prompts and task completion from the byte stream. Rate-limited, privacy-
  preserving (no project names in ntfy payloads).
- **Pluggable tunnel backend** — `local_only` (default), `quick_cloudflare`,
  `named_cloudflare`. Double-flag guard: you have to pick a Cloudflare backend
  *and* flip `enable_cloudflare=true` to actually go public.
- **Electron menu bar (macOS)** — tray icon, server start/stop, health polling,
  launch-at-login via LaunchAgent.
- **Docker (alternative)** — pure-container deployment for Linux / headless
  hosts. Python server + Claude CLI both run in the container.
- **Strict CSP** — no inline `<script>`, SVG allowlisted from `cdn.jsdelivr.net`,
  `frame-ancestors 'none'`, clickjack defense, no-referrer policy.

---

## Architecture

```
  ┌──────────────────────── REMOTE CLIENT (browser) ────────────────────────┐
  │   xterm.js  ·  TOTP login  ·  Launchpad (running + projects)  ·  D-pad  │
  └─────────────────────────────────┬───────────────────────────────────────┘
                                    │  HTTPS + WSS
                                    │  Authorization: Bearer <JWT>          (REST)
                                    │  Sec-WebSocket-Protocol: cloude.jwt.v1, <JWT>
                                    ▼
  ┌────────────────────────────── MAC HOST ─────────────────────────────────┐
  │                                                                         │
  │  ┌── Electron (menu bar) ──┐         ┌── FastAPI (uvicorn :8000) ──┐    │
  │  │ · tray icon             │ spawn   │ · /api/v1/auth/*            │    │
  │  │ · ServerManager         │────────▶│ · /api/v1/sessions/*        │    │
  │  │ · health poll /health   │◀────────│ · /ws/terminal              │    │
  │  │ · LaunchAgent installer │         │ · /health  (unauth)         │    │
  │  └─────────────────────────┘         │ · strict CSP middleware     │    │
  │                                      └──────────────┬──────────────┘    │
  │                                                     │                   │
  │       ┌────────────── SessionManager ───────────────┤                   │
  │       │ · single-active invariant                   │                   │
  │       │ · owned_tmux_sessions (persisted)           │                   │
  │       │ · adopt / detach / destroy flows            │                   │
  │       │ · _sanitize_tmux_name(project_name)         │                   │
  │       └────────────────────┬────────────────────────┘                   │
  │                            │                                            │
  │                            ▼                                            │
  │  ┌────────────────── SessionBackend (ABC) ────────────────────────┐     │
  │  │                                                                 │     │
  │  │   TmuxBackend  ───── tmux -L cloude  ──── pipe-pane ──► FIFO   │     │
  │  │       │                  │                                │    │     │
  │  │       │                  └── capture-pane (scrollback)    │    │     │
  │  │       │                                                   ▼    │     │
  │  │       │                                           tail loop ──►│ WS  │
  │  │       │                                                   ▲    │     │
  │  │       │ binary-safe write paths:                          │    │     │
  │  │       │   send-keys -l  (short, control-free UTF-8)       │    │     │
  │  │       │   send-keys -H  (short, control bytes / keys)     │    │     │
  │  │       │   load-buffer + paste-buffer -d -p  (large)       │    │     │
  │  │       │                                                        │     │
  │  │   PTYBackend   ────── fallback (no tmux on PATH) ───────        │     │
  │  └─────────────────────────────────────────────────────────────────┘     │
  │                                                                         │
  │  ┌── NotificationRouter ──┐    ┌── TunnelBackend (ABC) ──┐              │
  │  │ · IdleWatcher FSM      │    │ · local_only (default)  │              │
  │  │ · RateLimiter          │    │ · quick_cloudflare      │              │
  │  │ · ntfy.sh backend      │    │ · named_cloudflare      │              │
  │  └────────────────────────┘    └─────────────────────────┘              │
  └─────────────────────────────────────────────────────────────────────────┘
```

**Why a dedicated tmux socket.** `tmux -L cloude` spawns a tmux server distinct
from the user's default one. We never touch, list, or kill sessions on the
user's personal tmux. Everything Cloude Code does is scoped to our socket.

**Why the SessionBackend ABC.** Two backends ship: `TmuxBackend` (default when
`tmux` is on PATH; survives server restart) and `PTYBackend` (fallback; dies
with the parent). `build_backend()` reads `AuthConfig.session.backend`
(`auto` | `tmux` | `pty`) and degrades gracefully when tmux is missing.

**Why the NotificationRouter has a queue.** `emit()` is synchronous and
non-blocking — it's called from the PTY chunk handler, which is load-bearing
for terminal streaming. The async worker drains the queue, rate-limits, and
fires ntfy POSTs. Send failures never propagate.

---

## File structure

```
cloudecode/
├── macOS/                               # Electron menu-bar app
│   ├── main.js                          # Tray icon, app lifecycle, About dialog
│   ├── server-manager.js                # Python subprocess lifecycle + health poll
│   ├── launchagent-installer.js         # LaunchAgent plist install/uninstall
│   ├── preload.js                       # Secure IPC bridge
│   ├── package.json                     # Electron + electron-builder config
│   └── assets/                          # Tray icon, app icon
│
├── src/                                 # Python backend
│   ├── main.py                          # FastAPI app, lifespan, CSP middleware
│   ├── config.py                        # pydantic-settings + AuthConfig/SessionConfig/TunnelConfig
│   ├── models.py                        # pydantic request/response models
│   ├── api/
│   │   ├── routes.py                    # REST endpoints (sessions, tunnels, projects)
│   │   ├── auth.py                      # TOTP verify + JWT refresh + slowapi
│   │   ├── deps.py                      # WS subprotocol auth helper
│   │   └── websocket.py                 # /ws/terminal + resize handshake
│   ├── core/
│   │   ├── session_backend.py           # SessionBackend ABC + build_backend()
│   │   ├── session_manager.py           # single-active invariant + adopt/detach/destroy
│   │   ├── tmux_backend.py              # tmux -L cloude impl + binary-safe writes
│   │   ├── refresh_store.py             # aiosqlite JWT refresh-token store
│   │   ├── log_monitor.py               # pattern detection on pane output
│   │   ├── auto_tunnel.py               # auto-tunnel orchestrator
│   │   ├── notifications/
│   │   │   ├── router.py                # bounded-queue dispatcher + rate limiter
│   │   │   ├── idle_watcher.py          # FSM: prompt / permission / task-complete
│   │   │   ├── rate_limit.py            # global cap + per-kind cooldown
│   │   │   ├── ntfy.py                  # ntfy.sh backend (privacy-preserving)
│   │   │   └── events.py                # EventType + NotificationEvent
│   │   └── tunnel/
│   │       ├── manager.py               # TunnelManager router
│   │       └── backends/
│   │           ├── base.py              # TunnelBackend ABC
│   │           ├── local_only.py        # LAN-only (default)
│   │           ├── quick_cloudflare.py  # *.trycloudflare.com
│   │           └── named_cloudflare.py  # persistent Cloudflare named tunnel
│   └── utils/                           # pty_session, templates, patterns
│
├── client/                              # Web frontend (vanilla JS)
│   ├── index.html                       # SPA shell (strict CSP)
│   ├── js/
│   │   ├── api.js                       # REST + WS client
│   │   ├── auth.js                      # TOTP login, JWT storage, refresh
│   │   ├── launchpad.js                 # Running sessions + existing projects
│   │   ├── terminal.js                  # xterm.js integration
│   │   ├── dpad.js                      # Mobile D-pad controls
│   │   ├── slash-commands.js            # Slash command palette
│   │   ├── router.js                    # /session/<project> deep-link router
│   │   └── app.js                       # App controller
│   └── css/styles.css                   # Dark theme, responsive
│
├── tests/                               # pytest suite
│   ├── test_session_backend.py
│   ├── test_ws_subprotocol_auth.py
│   ├── test_refresh_tokens.py
│   ├── test_totp_rate_limit.py
│   ├── test_notifications.py
│   ├── test_rate_limiter.py
│   ├── test_idle_watcher.py
│   ├── test_tunnel_manager.py
│   └── test_deep_link_routing.py
│
├── docs/
│   ├── deployment-docker.md             # Mode 2 operator guide
│   └── superpowers/                     # Design specs + plans
│
├── scripts/preflight-bind-ip.sh         # Docker LAN-IP sanity check
├── Dockerfile                           # python:3.12-slim + tmux + Node + claude CLI
├── docker-compose.yml                   # Volume layout, UID/GID mapping
├── requirements.txt                     # Python deps
├── config.example.json                  # Reference config
├── .env.example                         # Reference env vars
├── setup.sh                             # venv + pip + cloudflared installer
├── setup_auth.py                        # Interactive TOTP/JWT/Cloudflare/ntfy wizard
├── start.sh / stop.sh / reset.sh / nuke.sh   # Shell helpers
└── README.md
```

---

## Prerequisites

| Requirement     | Version      | Notes                                                           |
| --------------- | ------------ | --------------------------------------------------------------- |
| macOS           | 13+          | Electron app targets recent macOS                               |
| Python          | 3.12         | pydantic-settings + slowapi + aiosqlite                         |
| tmux            | 3.2+         | `brew install tmux` — required for the tmux backend             |
| Node.js         | 20+          | Only required to build/run the Electron app                     |
| Claude CLI      | Latest       | `claude` on `PATH` or `CLAUDE_CLI_PATH` in `.env`               |
| cloudflared     | Any recent   | Only if you enable a Cloudflare tunnel backend                  |

Quick sanity check:

```bash
python3 --version          # >= 3.12
which tmux                 # expect /opt/homebrew/bin/tmux or /usr/local/bin/tmux
which claude               # expect a path; else export CLAUDE_CLI_PATH in .env
```

If tmux is missing the app falls back to `PTYBackend` — functional, but sessions
die with the server. Install tmux to get persistence.

---

## Installation

### Mode 1 — macOS native (Electron menu bar app) — PRIMARY

The default for macOS users. Electron owns the lifecycle, spawns the Python
server, talks to the local tmux socket. Claude Code runs natively on your Mac
with full access to:

- macOS Keychain (Claude Pro / Max OAuth tokens land here)
- macOS-native MCPs (Shortcuts, AppleScript, Calendar, Reminders, Messages)
- Your existing `~/.claude` directory, MCP server config, and shell environment

**End-user install (DMG):**

1. Grab `Cloude Code.dmg` from releases (or build from source — see below).
2. Open the DMG, drag to `/Applications`, launch.
3. First run copies default config to `~/Library/Application Support/cloude-code-menubar/`.
4. Click the menu bar icon → **Setup** to run the interactive auth wizard
   (generates TOTP + JWT secrets; prompts for optional Cloudflare / ntfy).
5. Scan the displayed QR code with your TOTP app.
6. Open `http://localhost:8000` or `http://<mac-lan-ip>:8000` from any device
   on the same LAN.

**Developer (clone + setup):**

```bash
git clone <repo-url> cloudecode
cd cloudecode

./setup.sh                    # creates venv, installs requirements, cloudflared (if needed)
python3 setup_auth.py         # interactive secrets + Cloudflare/ntfy wizard
./start.sh                    # starts uvicorn on 0.0.0.0:8000

# Optional — run the Electron menu bar app in dev mode:
cd macOS
npm install
npm start
```

### Mode 2 — Docker (pure container) — ALTERNATIVE

For Linux hosts, headless servers, or users who want full isolation. Both the
Python server AND the Claude Code CLI run inside the container.

```bash
cp .env.example .env
# Edit .env — at minimum set CLOUDE_BIND_IP (use scripts/preflight-bind-ip.sh
# to list live interfaces). Secrets (TOTP_SECRET, JWT_SECRET) can be minted by
# running setup_auth.py inside the container after first build.
bash scripts/preflight-bind-ip.sh
UID=$(id -u) GID=$(id -g) docker compose build
docker compose up -d

# Mint secrets interactively inside the running container:
docker exec -it cloude-cloude-1 python3 setup_auth.py
```

**Mode 2 caveats:**

- **Claude Pro / Max OAuth is NOT supported in Mode 2** — macOS Keychain isn't
  reachable from a Linux container. Use Mode 1 or set `ANTHROPIC_API_KEY` if
  you have direct API billing.
- **macOS-native MCPs do not work** — anything that calls Shortcuts,
  AppleScript, Calendar, Reminders, Messages, Finder, or any Keychain-backed
  service fails inside the container. Network-based MCPs (Gmail, GDrive, n8n,
  Postgres, HTTP) work fine.
- Default bind is `127.0.0.1` — the container is loopback-only unless you opt
  in to LAN exposure via `CLOUDE_BIND_IP`.

See `docs/deployment-docker.md` for the full walkthrough (volume layout,
UID/GID mapping, preflight script, per-container secrets).

### Hybrid "server-in-container, Claude-on-host" — NOT SHIPPED

A third mode — FastAPI in Docker, Claude CLI on the host via a UDS-to-tmux
bridge — was evaluated and cut. Docker Desktop's LinuxKit VM boundary doesn't
pass live Unix sockets reliably, and the UID-match + LaunchDaemon complexity
didn't justify the reward when Mode 1 already covers the "Claude on host"
case natively. Mode 1 on macOS, Mode 2 on Linux. That's it.

---

## Configuration

### `.env` — runtime environment

Copy `.env.example` → `.env`. `setup_auth.py` populates secrets and prompts for
optional Cloudflare/ntfy values.

| Variable                 | Required           | Default          | Purpose                                                       |
| ------------------------ | ------------------ | ---------------- | ------------------------------------------------------------- |
| `HOST`                   | No                 | `0.0.0.0`        | uvicorn bind address (use `127.0.0.1` for loopback-only)      |
| `PORT`                   | No                 | `8000`           | uvicorn port                                                  |
| `DEFAULT_WORKING_DIR`    | **Yes**            | —                | Directory where new project sessions are created              |
| `LOG_DIRECTORY`          | **Yes**            | —                | `session_metadata.json`, `refresh_tokens.db`, pipe FIFOs      |
| `SESSION_TIMEOUT`        | No                 | `3600`           | Session inactivity timeout (seconds)                          |
| `LOG_BUFFER_SIZE`        | No                 | `1000`           | In-memory log line buffer                                     |
| `LOG_FILE_RETENTION`     | No                 | `7`              | Days to retain rotated pipe files                             |
| `CLAUDE_CLI_PATH`        | No                 | auto-detect      | Absolute path to `claude` binary                              |
| `TOTP_SECRET`            | **Yes**            | generated        | Generated by `setup_auth.py` — do not edit manually           |
| `JWT_SECRET`             | **Yes**            | generated        | Generated by `setup_auth.py` — do not edit manually           |
| `ALLOWED_ORIGINS`        | No                 | `["*"]`          | CORS allowlist — JSON array or comma-separated                |
| `AUTH_CONFIG_FILE`       | No                 | `./config.json`  | Path to projects + slash commands + feature config            |
| `CLOUDFLARE_API_TOKEN`   | If `named_cloudflare` | —             | Needs `Zone.DNS:Edit` + `Cloudflare Tunnel:Edit`              |
| `CLOUDFLARE_ZONE_ID`     | If `named_cloudflare` | —             | Zone ID for your domain                                       |
| `CLOUDFLARE_DOMAIN`      | If `named_cloudflare` | —             | e.g. `cloude.example.com`                                     |
| `CLOUDFLARE_TUNNEL_NAME` | No                 | `claude-tunnel`  | Name used for the named tunnel                                |
| `CLOUDE_BIND_IP`         | Docker only        | `127.0.0.1`      | Host IP to publish port 8000 on (Mode 2)                      |
| `CLOUDE_PROJECT_PATH`    | Docker only        | `./projects`     | Host path mounted as `/workspace` in the container            |
| `CLOUDE_LOG_DIR`         | Docker only        | `./logs`         | Host path for state (`refresh_tokens.db`, FIFOs)              |

### `config.json` — feature configuration

Everything that's not a secret lives here. `AuthConfig` composes five sub-blocks
loaded by `src/config.py::Settings.load_auth_config()`:

**`session` (`SessionConfig`)**

| Key                 | Type    | Default    | Purpose                                                        |
| ------------------- | ------- | ---------- | -------------------------------------------------------------- |
| `backend`           | str     | `"auto"`   | `"auto"` (tmux if present else pty) / `"tmux"` / `"pty"`       |
| `tmux_socket_name`  | str     | `"cloude"` | Passed to `tmux -L <name>` — dedicated socket                  |
| `scrollback_lines`  | int     | `3000`     | Lines captured on re-attach                                    |

**`tunnel` (`TunnelConfig`)**

| Key                  | Type  | Default        | Purpose                                                                    |
| -------------------- | ----- | -------------- | -------------------------------------------------------------------------- |
| `backend`            | str   | `"local_only"` | `"local_only"` / `"quick_cloudflare"` / `"named_cloudflare"`               |
| `enable_cloudflare`  | bool  | `false`        | Master switch — must be `true` for Cloudflare backends (double-flag guard) |
| `lan_hostname`       | str   | `"auto"`       | Override LAN host for `local_only` (`"auto"` = detect)                     |

**`auth_rate_limits` (`AuthRateLimits`)**

| Key                      | Type | Default | Purpose                                                            |
| ------------------------ | ---- | ------- | ------------------------------------------------------------------ |
| `totp_verify_per_minute` | int  | `5`     | slowapi limit on `POST /api/v1/auth/verify`                        |
| `totp_verify_per_hour`   | int  | `20`    | Second-window cap (both must hold)                                 |
| `trust_proxy_headers`    | bool | `false` | Honor `X-Forwarded-For` (only behind a trusted reverse proxy)      |

**`notifications` (`NotificationsConfig`)**

| Key                                      | Type  | Default             | Purpose                                                            |
| ---------------------------------------- | ----- | ------------------- | ------------------------------------------------------------------ |
| `enabled`                                | bool  | `false`             | Master switch — when false, emit is a no-op                        |
| `ntfy_base_url`                          | str   | `"https://ntfy.sh"` | ntfy server (override for self-hosted)                             |
| `ntfy_topic`                             | str   | `""`                | Treat as a credential — 32 hex bytes from `setup_auth.py`          |
| `public_base_url`                        | str   | `""`                | Used in the ntfy `Click` header for deep-link                      |
| `idle_threshold_seconds`                 | float | `30.0`              | Silence before IdleWatcher fires `TASK_COMPLETE`                   |
| `rate_limit_global_cap`                  | int   | `10`                | Notifications per `rate_limit_window_seconds`                      |
| `rate_limit_window_seconds`              | float | `60.0`              | Rolling-window duration                                            |
| `rate_limit_per_kind_cooldown_seconds`   | float | `10.0`              | Minimum seconds between two emits of the same EventType            |

**Top-level (`AuthConfig`)**

| Key                         | Type | Default   | Purpose                                                         |
| --------------------------- | ---- | --------- | --------------------------------------------------------------- |
| `access_token_ttl_seconds`  | int  | `900`     | JWT access token lifetime (15 min)                              |
| `refresh_token_ttl_seconds` | int  | `604800`  | JWT refresh token lifetime (7 days)                             |
| `refresh_grace_seconds`     | int  | `10`      | Window a just-rotated refresh can still be used                 |
| `jwt_expiry_minutes`        | int  | `30`      | Legacy — only honored if access TTL unset                       |
| `template_path`             | str  | `null`    | Path to template files copied on new session (if opted in)      |
| `projects`                  | list | `[]`      | Launchpad projects (name, path, description)                    |
| `common_slash_commands`     | list | `[]`      | Slash-command palette entries                                   |

See `config.example.json` for a complete reference instance.

---

## Running

### Dev (Python only)

```bash
source venv/bin/activate
python3 -m src.main          # or ./start.sh
```

uvicorn listens on `0.0.0.0:8000`. From a phone on the same LAN, hit
`http://<mac-lan-ip>:8000`.

### Dev (Electron + server)

```bash
cd macOS
npm start
```

Electron spawns the Python server. If a server is already running on 8000,
`ServerManager` adopts it instead of spawning a duplicate. The tray icon
polls `/health` every 5 seconds (2 seconds during startup).

### Production (packaged DMG)

Launch **Cloude Code.app** from `/Applications`. First-run copies default
config to `~/Library/Application Support/cloude-code-menubar/`.

### Shell helpers

| Script        | Purpose                                                                                |
| ------------- | -------------------------------------------------------------------------------------- |
| `start.sh`    | Activates venv and starts the Python server                                            |
| `stop.sh`     | Graceful server shutdown                                                               |
| `reset.sh`    | Light reset — stops server, clears session metadata, preserves `.env` + `config.json`  |
| `nuke.sh`     | Destructive: deletes `.env`, `config.json`, `venv/`, Cloudflare tunnels, DNS records   |

`nuke.sh` deletes remote Cloudflare resources. Review before running on a shared
account.

---

## Launching Claude with a custom alias

If you use a custom shell alias or function to launch Claude (e.g. `cld` for
`claude --dangerously-skip-permissions`), you can't just pass the alias name
as the tmux session's inline command — tmux spawns a *non-interactive*
shell for inline commands, which does NOT source your `~/.zshrc` or
`~/.bashrc`. Aliases defined there are invisible. The `exec $SHELL` tail
drops you into an interactive shell *after* the command fails, which is
what makes the failure mode extra misleading — you land at a prompt where
`cld` works fine, but the launcher already bailed with `command not found`.

The fix: force tmux to spawn an *interactive* shell via `$SHELL -ic '...'`.
The `-i` flag tells the shell to source your rc file before running the
command.

### Quick form

Run this directly from any terminal on the Mac hosting Cloude Code:

```bash
tmux -L cloude new -s mywork "$SHELL -ic 'cld; exec $SHELL'"
```

Breakdown:
- `-L cloude` — tmux's dedicated socket for Cloude Code (required for the
  web UI to discover the session)
- `-s mywork` — the session name (will appear in the launchpad's
  "Adopt an external session" list)
- `$SHELL -ic '...'` — interactive shell, sources `~/.zshrc` or `~/.bashrc`
- `cld; exec $SHELL` — run your custom launcher, then when it exits
  replace the shell process with a fresh interactive shell so the pane
  stays alive and you land at a prompt

### Reusable shell function

Add this to your `~/.zshrc` or `~/.bashrc` so you can launch with one short
command:

```bash
cloude() {
    local name="${1:-mywork}"
    local dir="${2:-$PWD}"
    tmux -L cloude new -s "$name" -c "$dir" "$SHELL -ic 'cld; exec $SHELL'"
}
```

Usage:

```bash
cloude                                    # session "mywork" in current dir
cloude api                                # session "api" in current dir
cloude api ~/projects/some-repo           # session "api" in that repo
```

Detach the CLI with `Ctrl+B d` and the Cloude Code launchpad will list the
session under "Adopt an external session".

### If your launcher is a function, not an alias

Shell functions defined in your rc file work the same way — `$SHELL -ic`
sources the rc and makes the function available:

```bash
# in ~/.zshrc
claude-fast() {
    claude --dangerously-skip-permissions --model opus-4-7 "$@"
}

# then:
tmux -L cloude new -s mywork "$SHELL -ic 'claude-fast; exec $SHELL'"
```

### Why not source ~/.zshrc directly?

You can — `"source ~/.zshrc && cld; exec $SHELL"` also works. But `-ic` is
shorter, matches the mental model of "open an interactive shell and run
this," and handles both zsh and bash uniformly without caring which rc
file lives where.

### Why not put the alias in ~/.zshenv?

`~/.zshenv` IS sourced by non-interactive shells, so the original
`tmux -L cloude new -s mywork "cld; exec $SHELL"` form would work if `cld`
lives there. But `.zshenv` runs for every zsh invocation including scripts,
so putting slow stuff there is painful. Aliases are cheap — your call.

---

## API reference

Base URL: `http://<host>:8000` · REST prefix: `/api/v1`

### Unauthenticated

| Method | Path                       | Body                 | Returns                                    |
| ------ | -------------------------- | -------------------- | ------------------------------------------ |
| `GET`  | `/health`                  | —                    | `{ status, session_active, monitoring }`   |
| `GET`  | `/api/v1/health`           | —                    | `HealthResponse` (menu-bar uses this)      |
| `GET`  | `/api/v1/auth/qr`          | —                    | `{ qr_image, secret, uri }` (data URL PNG) |
| `POST` | `/api/v1/auth/verify`      | `VerifyTOTPRequest`  | `AuthTokenResponse` (access + refresh)     |
| `POST` | `/api/v1/auth/refresh`     | `{ refresh_token }`  | `AuthTokenResponse`                        |
| `POST` | `/api/v1/auth/logout`      | `{ refresh_token }`  | `SuccessResponse`                          |

### Authenticated — `Authorization: Bearer <access_jwt>`

| Method   | Path                                 | Body                     | Returns                    |
| -------- | ------------------------------------ | ------------------------ | -------------------------- |
| `POST`   | `/api/v1/sessions`                   | `CreateSessionRequest`   | `Session`                  |
| `GET`    | `/api/v1/sessions`                   | —                        | `SessionInfo`              |
| `DELETE` | `/api/v1/sessions`                   | —                        | `SuccessResponse` (destroys) |
| `POST`   | `/api/v1/sessions/detach`            | —                        | `SuccessResponse`           |
| `GET`    | `/api/v1/sessions/attachable`        | —                        | `List[AttachableSession]`  |
| `POST`   | `/api/v1/sessions/adopt`             | `AdoptSessionRequest`    | `AdoptSessionResponse`     |
| `POST`   | `/api/v1/sessions/command`           | `CommandRequest`         | `SuccessResponse`          |
| `GET`    | `/api/v1/sessions/logs?limit=N`      | —                        | `List[LogEntry]`           |
| `GET`    | `/api/v1/tunnels`                    | —                        | `List[Tunnel]`             |
| `POST`   | `/api/v1/tunnels`                    | `CreateTunnelRequest`    | `Tunnel`                   |
| `DELETE` | `/api/v1/tunnels/{id}`               | —                        | `SuccessResponse`          |
| `GET`    | `/api/v1/projects`                   | —                        | `List[ProjectResponse]`    |
| `POST`   | `/api/v1/projects`                   | `CreateProjectRequest`   | `ProjectResponse`          |
| `DELETE` | `/api/v1/projects/{name}`            | —                        | `SuccessResponse`          |
| `GET`    | `/api/v1/filesystem/browse?path=...` | —                        | `BrowseResponse`           |
| `GET`    | `/api/v1/auth/status`                | —                        | `SuccessResponse`          |
| `GET`    | `/api/v1/config/common-commands`     | —                        | `{ commands: [...] }`      |
| `POST`   | `/api/v1/server/reset`               | —                        | `SuccessResponse`          |
| `POST`   | `/api/v1/shutdown`                   | —                        | `SuccessResponse`          |

### WebSocket — `/ws/terminal`

```
ws://<host>:8000/ws/terminal
Sec-WebSocket-Protocol: cloude.jwt.v1, <access_jwt>
```

Server validates the JWT BEFORE accepting, then echoes the `cloude.jwt.v1`
marker as the negotiated subprotocol (RFC 6455 §4.1). Close codes on failure:

- **4401** — missing marker / missing token / invalid token
- **4400** — `Sec-WebSocket-Protocol` header present but malformed (empty /
  whitespace-only)

On success the server sends `{ "type": "request_dims" }`; the client fits
xterm.js and replies `{ "type": "pty_resize", cols, rows }` bypassing its
normal 100ms debounce. The server resizes the tmux window, waits ~150ms for
SIGWINCH to propagate, and writes Ctrl+L (0x0c) to force a clean redraw.

**Message types (server → client)**

- `{"type": "request_dims"}` — resize handshake open
- Binary frames — raw pane bytes (post-handshake live stream)
- `{"type": "log", ...}` — system messages
- `{"type": "tunnel_created", "tunnel": Tunnel}` — auto-tunnel event
- `{"type": "session_status", ...}` — session state change
- `{"type": "pong"}` — keepalive reply

**Message types (client → server)**

- Binary frames — raw input bytes (keystrokes, paste)
- `{"type": "pty_resize", "cols": N, "rows": N}` — resize
- `{"type": "ping"}` — keepalive

---

## Authentication flow

```
      Client                              Server
        │                                   │
        │  GET /api/v1/auth/qr              │   (unauth)
        │──────────────────────────────────▶│
        │  { qr_image, secret, uri }        │
        │◀──────────────────────────────────│
        │                                   │
        │  [scan QR with TOTP app]          │
        │                                   │
        │  POST /api/v1/auth/verify         │
        │  { code: "123456" }               │
        │──────────────────────────────────▶│
        │                                   │   slowapi: 5/min, 20/hour
        │                                   │   TTLCache replay dedup (90s)
        │                                   │   pyotp.verify(valid_window=1)
        │  { access_token, refresh_token,   │   mint access + refresh pair
        │    expires_in }                   │   persist refresh jti in SQLite
        │◀──────────────────────────────────│
        │                                   │
        │  Authorization: Bearer <access>   │
        │  GET /api/v1/sessions             │
        │──────────────────────────────────▶│   jwt.decode + typ=="access"
        │                                   │
        │  on 401 expired:                  │
        │  POST /api/v1/auth/refresh        │
        │  { refresh_token }                │
        │──────────────────────────────────▶│   rotate in SQLite
        │  { access_token, refresh_token }  │   detect reuse → revoke chain
        │◀──────────────────────────────────│
        │                                   │
        │  WS /ws/terminal                  │
        │  Sec-WebSocket-Protocol:          │
        │    cloude.jwt.v1, <access>        │
        │═══════════════════════════════════│
        │  (token NEVER in URL)             │
```

**Access token** — 15 min default. HS256. Payload: `exp, iat, sub, typ="access"`.
Explicit `algorithms=["HS256"]` on decode (RFC 8725 §3.2 guard against
`"alg": "none"`). Wrong `typ` → 401 (no refresh-token smuggling into
`Authorization` headers).

**Refresh token** — 7 days default. Has a random `jti` (32 url-safe bytes)
persisted in SQLite (`RefreshStore`). On `/auth/refresh`, the server rotates:
issues new access + refresh, marks old as superseded, returns the pair.
Detection: if a superseded refresh shows up past the grace window (10s
default), the whole chain from that jti forward is revoked and the user
must re-TOTP. Benign race (two refreshes in-flight inside the grace window)
returns 401 with "already rotated; retry" — client just uses its freshest
token.

**Replay defense** — a 90s TTLCache (covers pyotp's ±1 step window plus
buffer) dedups submitted TOTP codes. Serialized under an asyncio lock to
prevent TOCTOU where two concurrent submissions of the same code both
slip through.

---

## Tmux integration

### Socket

All tmux operations use `tmux -L cloude`. This spawns a tmux server that's
completely separate from the user's default server. Cloude Code never sees,
lists, or touches the user's personal tmux sessions.

### Naming

Sessions are named verbatim after the project:

```
project.name = "Cloude Code Dev"
    │
    ▼  _sanitize_tmux_name()
       - replace  .  →  _   (tmux pane separator)
       - replace  :  →  _   (tmux window separator)
       - collapse whitespace runs to a single space
       - strip leading/trailing whitespace
    ▼
sanitized  = "Cloude Code Dev"
    │
    ▼
tmux session = "cloude_Cloude Code Dev"
```

Case, spaces, emoji, punctuation — all preserved. tmux tolerates them.

Legacy sessions named `cloude_ses_<hex>` (pre–v0.5) are still supported and
co-exist with verbatim-named sessions. No migration is performed.

### Adopt-on-collision

When the user clicks a project whose verbatim tmux name already exists on the
socket, `create_session` redirects to `adopt_external_session` with
`confirm_detach=True`. "Open project X" means "resume my X session, alive or
not." The probe is read-only (`list-sessions`), so checking for collision
has no side effects.

### Discover / rehydrate

On server startup, `SessionManager.lifespan_startup()` runs a probe backend to
list `cloude_*` sessions on the socket. If a session from `session_metadata.json`
is present AND its name is in `owned_tmux_sessions`, the backend attaches to the
live session. Stale entries get pruned. Legacy (pre-v3) metadata without
`owned_tmux_sessions` triggers a one-shot backfill.

### Binary-safe writes

`TmuxBackend.write(data)` routes through three paths based on payload shape:

| Condition                                   | tmux command                                        | Why                                                                       |
| ------------------------------------------- | --------------------------------------------------- | ------------------------------------------------------------------------- |
| Short (≤ 256 B), no control chars           | `send-keys -l <text>`                               | Literal UTF-8, fast path for typing                                       |
| Short, has control chars (0x03, 0x1b, etc.) | `send-keys -H <hex pairs>`                          | Each hex pair = one byte delivered as a key event (arrows, Ctrl-X, Esc)   |
| > 256 B                                     | `load-buffer` + `paste-buffer -d -p`                | Bracketed-paste markers so Claude distinguishes paste from typed input    |

`send-keys -H` is the only correct path for keystrokes like Backspace (0x7f),
Escape (0x1b), arrows (`\x1b[A..D`), Ctrl chords, and F-keys. `send-keys -l`
would treat them as literal characters; `paste-buffer` would wrap them in
paste markers. Three paths exist because there is no single tmux command that
handles all three cases correctly.

### Output streaming

`tmux pipe-pane -o 'cat >> <fifo>'` streams every pane byte to a file under
`LOG_DIRECTORY` (e.g. `tmux_cloude_myproject.pipe`). `TmuxBackend._tail_loop`
opens the file with `O_NONBLOCK`, seeks to EOF (or to the recorded adopt
offset), and fans bytes out via `on_output`. Rotation: 10 MiB cap or 24 hour
age, rename to `.1`, truncate.

### Window size (the 80x24 bug that's not a bug)

We never attach a tmux client — output is streamed via `pipe-pane`. Without a
client, tmux has no dims to derive window size from, so it pins the window at
its 80x24 birth size forever. Two settings fix this:

- `-x / -y` on `new-session` sets the birth geometry.
- `set-option window-size manual` locks it so `resize-window` is the *only*
  thing that changes size (no auto-sizing surprises).

Resize on WS connect uses `resize-window -x -y` (server-side, emits SIGWINCH
to the foreground process). `refresh-client -C` is a no-op for us because
we have no client.

---

## Invariants

These hold across the whole design. Violating any of them is a bug.

- **Never kill on switch.** Switching sessions calls
  `detach_current_session` (tears down Python-side handles, stops our
  pipe-pane, leaves tmux alive). The *only* kill paths are the X button in
  the UI (calls `DELETE /api/v1/sessions`), the destroy button on the
  terminal view, and manual `tmux -L cloude kill-session` in a shell.
- **Single active session.** `SessionManager` holds at most one
  `SessionBackend` at a time. `create_session` raises if there's already a
  live session. Swapping requires explicit `confirm_detach=True` on
  `POST /sessions/adopt`.
- **`owned_tmux_sessions` persists across restart.** The set of cloude-
  created session names is part of `session_metadata.json`. On startup,
  `lifespan_startup` reconciles it against the live tmux listing,
  pruning stale entries. Adopt UI uses this set to flag owned-vs-external,
  not a spoofable `cloude_` prefix match.
- **Replay flag around scrollback.** `backend.replay_in_progress = True`
  while streaming historical bytes so IdleWatcher and pattern detection
  don't see them as new output.
- **No token in URL.** WebSocket JWT rides on `Sec-WebSocket-Protocol`,
  never `?token=`. Query strings get logged; subprotocol headers don't.

---

## Notifications

Opt-in. Off by default (`notifications.enabled = false`). Single backend:
ntfy.sh.

### Event kinds

| EventType           | When it fires                                                       | Priority |
| ------------------- | ------------------------------------------------------------------- | -------- |
| `PERMISSION_PROMPT` | Claude asks for approval — detected synchronously on stream         | 5        |
| `INPUT_REQUIRED`    | Session is blocked on user input                                    | 4        |
| `TASK_COMPLETE`     | Pane went quiet for `idle_threshold_seconds` on a prompt frame      | 3        |
| `ERROR`             | Error pattern detected                                              | 3        |
| `BUILD_COMPLETE`    | Build-success pattern detected                                      | 3        |
| `TEST_RESULT`       | Test runner finished                                                | 3        |
| `TUNNEL_CREATED`    | Auto-tunnel brought a port online                                   | 3        |

### IdleWatcher FSM

`TASK_COMPLETE` is the hard one. `IdleWatcher` maintains a 16KB ring buffer
of recent pane output, strips ANSI, and classifies the tail. It fires
`TASK_COMPLETE` only when BOTH `╭─╮` (top) and `╰─╯` (bottom) corners of
a Claude Code prompt frame are visible AND the session has been silent for
`idle_threshold_seconds`.

False-positive guards:
- `╭─╮` alone matches rendered markdown boxes → require both corners
- `Allow` unanchored matches grep output → anchor to line-start + menu item
- `^C` echo during Ctrl-C → suspend idle detection (INTERRUPTED state)

### Rate limiting

Two limits gate every emit:
- **Global cap** — `rate_limit_global_cap` notifications per
  `rate_limit_window_seconds` (default 10/60s).
- **Per-kind cooldown** — minimum seconds between two emits of the same
  `EventType` (default 10s). Dedups bursts like repeated error matches.

The limiter seeds its per-kind timestamps at cold start so a notification
storm racing startup (e.g., scrollback slipping past the replay guard) gets
swallowed by the cooldown.

### Privacy contract

Project names and session slugs NEVER appear in ntfy `Title` / `Body` / `Tags`.
Generic titles ("Cloude: permission requested"), generic bodies ("Tap to open
session."). The slug DOES appear in the `Click` header URL
(`{public_base_url}/session/<slug>`) — accepted trade-off under the LAN-only
threat model.

### Setup

```bash
python3 setup_auth.py          # prompts for ntfy config, mints a 32-hex topic
```

The topic IS the credential. Anyone who knows the topic name can read your
notifications. Treat it like a secret. Self-host ntfy if you don't trust
sh.ntfy.sh.

---

## Deployment modes

**Mode 1 — macOS native (Electron menu bar).** Primary, best-supported. Full
macOS integration — Keychain, native MCPs, shell env, `~/.claude`. Install
via DMG or `npm run build` in `macOS/`. Use this unless you have a reason
not to.

**Mode 2 — Docker (pure container).** Alternative. Linux hosts, headless
servers, or isolated environments. Both the Python server and the Claude CLI
run in the container. No macOS Keychain means no Claude Pro / Max OAuth;
`ANTHROPIC_API_KEY` works. No macOS-native MCPs. Network MCPs (Gmail,
GDrive, Postgres, HTTP) all work. See `docs/deployment-docker.md`.

**Hybrid (server-in-container, Claude-on-host) — CUT.** Docker Desktop's
LinuxKit VM doesn't pass live Unix sockets from the host to the container
reliably. The UID-match + LaunchDaemon complexity didn't clear the
complexity bar. Mode 1 already runs Claude natively; the hybrid never had
a unique value prop.

---

## Development

```bash
./setup.sh                         # venv + pip + cloudflared
source venv/bin/activate
python3 setup_auth.py              # generate .env + config.json
python3 -m src.main                # dev server (reload=True)

# in another terminal
pytest tests/ -v
```

Test suite covers:
- `test_session_backend.py` — ABC + factory + tmux vs pty selection
- `test_ws_subprotocol_auth.py` — WS JWT handshake + close codes
- `test_refresh_tokens.py` — rotation + reuse detection + chain revocation
- `test_totp_rate_limit.py` — slowapi + TTLCache replay dedup
- `test_notifications.py` / `test_rate_limiter.py` / `test_idle_watcher.py` —
  notification pipeline
- `test_tunnel_manager.py` — backend selection + double-flag guard
- `test_deep_link_routing.py` — `/session/<project>` deep link

Tmux-adjacent tests skip cleanly when tmux isn't on PATH. Install tmux for
full coverage.

### Electron dev

```bash
cd macOS
npm install
npm start                          # dev mode — spawns Python server
npm run build                      # produces dist/Cloude Code.dmg
```

---

## Known issues and residual risks

| Issue                                      | Mitigation                                                               | Status         |
| ------------------------------------------ | ------------------------------------------------------------------------ | -------------- |
| Tunnel URL is public                       | TOTP + JWT on every API route; Cloudflare Access in front is recommended | Documented     |
| `ALLOWED_ORIGINS = ["*"]` out of the box   | Restrict to your LAN origin in `.env`                                    | Documented     |
| ntfy topic is a shared credential          | Treat like a password; self-host ntfy if you don't trust sh.ntfy.sh      | Documented     |
| PTY runs unsandboxed                       | Single-user LAN model; don't share access with untrusted parties         | Accepted       |
| Legacy `cloude_ses_<hex>` sessions         | Continue to work; no migration                                           | By design      |
| Menu bar "Stopped" while server is running | Health poll adopts existing process on port 8000                         | Partial fix    |
| `CLOUDFLARE_DOMAIN` placeholder after setup| UI surfaces "Setup Required" state on placeholder detection              | Workaround     |
| Docker Desktop Unix-socket passthrough     | Hybrid mode was cut — use Mode 1 or Mode 2                               | Won't fix      |

---

## Troubleshooting

### Server won't start

- `lsof -i :8000` — Electron should adopt an existing process. If not, kill
  the orphan or `./stop.sh`.
- `.env` missing or incomplete → re-run `setup_auth.py`. Packaged app reads
  `~/Library/Application Support/cloude-code-menubar/.env`.
- `which python3` → must exist; install via `brew install python@3.12`.

### TOTP rejected

- Clock drift: `sudo sntp -sS time.apple.com` to resync.
- Wrong secret: re-run `setup_auth.py` and re-scan the QR.
- Rate-limited: 5/min, 20/hour by default. Wait for the `Retry-After` header,
  then retry.

### WS connection drops immediately

- Check the browser console for close code:
  - **4401** — bad JWT (expired, wrong `typ`, or missing). Re-log-in.
  - **4400** — malformed `Sec-WebSocket-Protocol`. Client bug.
- Verify `cloude.jwt.v1` marker is the first subprotocol in the client's
  array (most browsers tolerate either order; some proxies don't).

### Session lost after server restart

- `ls $LOG_DIRECTORY/session_metadata.json` — exists? If yes, tmux is
  probably dead; check `tmux -L cloude list-sessions`.
- `tmux -L cloude list-sessions` — if empty, metadata points to a dead
  session and will be pruned on next startup.
- If tmux is alive but not re-attaching: check server logs for
  `session_metadata_slug_not_owned` — the session isn't in
  `owned_tmux_sessions` and the launchpad will offer it as adoptable instead.

### Terminal renders at 80x24

- WS resize handshake failed or timed out (2s budget). Refresh the browser
  — a fresh WS connect triggers a new handshake.
- If persistent, check `window-size` via `tmux -L cloude show-options -sv
  window-size` — must be `manual`. `start()` sets this; an external session
  started without it will log a warning at adopt time.

### Adopt-external session doesn't appear

- Must be on the cloude socket: `tmux -L cloude new -s mywork` (NOT `tmux new`).
- Launchpad queries `GET /api/v1/sessions/attachable`; check browser devtools
  for the response.
- Session name contains `.` or `:` → tmux target parsing rejects it. Rename.

### Claude CLI doesn't start in a session

- `which claude` — must return a path; else set `CLAUDE_CLI_PATH` in `.env`.
- `claude --help` — should work without OAuth prompts.
- Sessions call `claude --dangerously-skip-permissions` — that's deliberate
  for the headless-terminal workflow.

### Can't connect from phone

- Same LAN required for direct access (`http://<mac-lan-ip>:8000`).
- macOS firewall: **System Settings → Network → Firewall** must allow port 8000.
- `ifconfig | grep 'inet '` to find the Mac's LAN IP.
- Tailscale / UniFi Teleport: hit the overlay hostname instead.

---

## Architecture evolution

Short version of how we got here. Commit messages tell the full story.

- **PTY → tmux.** The v0.1 MVP used a raw `pty` fork. Sessions died with the
  server, which turned every Electron restart into a lost session. Switched
  to a dedicated tmux socket (`tmux -L cloude`) so sessions survive restarts
  and can be re-adopted from the launchpad on next boot.
- **Banner → unified running sessions.** Launchpad originally had three
  sections: "active session banner", "adopt external", "existing projects".
  Conceptually overlapping. Collapsed into two: **Running sessions** (owned
  + external in one list, pulsing status dots, inline X destroy) and
  **Existing projects**. The banner is gone.
- **Destroy-on-swap → detach-on-swap.** Early design killed the prior session
  when switching. Terrifying UX: accidentally click a different project,
  lose work. Now switching *detaches* — tmux stays alive, the prior session
  re-appears in the running list, re-adoptable. Only explicit X button kills.
- **Slug → verbatim naming.** Sessions used to be named `cloude_ses_<8-hex>`
  from a UUID. Meaningless in the launchpad. Now `cloude_<project name>`
  verbatim — tmux allows spaces, emoji, punctuation; only `.` and `:` get
  sanitized (they're tmux target separators). Legacy hex names still
  supported.
- **Scrollback replay → resize handshake.** Replaying stored bytes on WS
  reconnect meant painting at the previous geometry — visible corruption
  whenever the new client had different dims. Replaced with a resize
  handshake on connect: server requests dims, client replies, backend
  resizes, Ctrl+L forces a clean redraw. User loses historical scrollback
  on reconnect; xterm.js retains client-side history within a page load
  anyway.
- **Query-string JWT → WS subprotocol.** Tokens used to ride in `?token=`.
  That leaks into proxy access logs. Now JWT is a `Sec-WebSocket-Protocol`
  value; server validates pre-accept and echoes the marker back.
- **Single access token → access + refresh pair.** Short-lived access (15m)
  limits blast radius of a leak; long-lived refresh (7d) with SQLite
  persistence, rotation, reuse detection, and chain revocation.
- **HybridTunnelManager → pluggable TunnelBackend ABC.** One class grew to
  handle local + quick + named + DNS. Refactored to a `TunnelBackend` ABC
  with `local_only`, `quick_cloudflare`, `named_cloudflare` implementations
  selected by `tunnel.backend` config. Double-flag guard requires
  `enable_cloudflare=true` in addition to picking a Cloudflare backend.
- **v0.2 → v0.5.** Version bump reflects the weekend-MVP → hardened-LAN-app
  transition. Menu-bar status dot now polls `/health` directly.

---

## Contributing

Pull requests welcome. For substantial changes, open an issue first.

```bash
git checkout -b feature/your-feature
# ...make changes, run tests...
pytest tests/ -v
git commit -am "feat: description"
git push origin feature/your-feature
# open PR
```

Keep diffs focused. Don't break the invariants. If you're touching the tmux
backend, run the full `test_session_backend.py` + `test_ws_subprotocol_auth.py`
suite on a machine with tmux installed.

---

## License

MIT — see `LICENSE` file.

---

Built for developers who want to code from anywhere. No more being chained to
your desk.
