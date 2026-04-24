# Mode 2 — Docker Deployment

Operator-facing guide for running Cloude Code as a pure container. The FastAPI server and the Claude Code CLI both run inside the container; your host only ships Docker Desktop and a browser.

If you're on macOS and want Claude Pro / Max OAuth or macOS-native MCPs, use [Mode 1](../README.md#mode-1--macos-native-electron-menu-bar-app-primary) instead.

---

## 1. Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Docker Desktop | 4.x+ | or Docker Engine 24+ on Linux |
| Host shell | bash / zsh | `id -u` and `id -g` must resolve |
| Free port | `8000` | published on the host side |
| Disk | ~2 GB | image + logs + session state |

Network MCP endpoints you plan to use (ntfy.sh, Postgres, custom HTTP) must be reachable from inside the container.

---

## 2. First-time setup

```bash
git clone <repo-url> cloudecode
cd cloudecode
cp .env.example .env
```

Generate the two required secrets. Easiest path is to spin the container once and let `setup_auth.py` do it:

```bash
UID=$(id -u) GID=$(id -g) docker compose build
docker compose run --rm cloude python3 setup_auth.py
```

Or mint them manually:

```bash
python3 -c 'import secrets; print("JWT_SECRET=" + secrets.token_urlsafe(48))'
python3 -c 'import pyotp; print("TOTP_SECRET=" + pyotp.random_base32())'
```

Paste the results into `.env`. Then set your LAN bind IP if you want phone access:

```bash
# .env
CLOUDE_BIND_IP=192.168.1.250       # or leave unset for loopback-only
CLOUDE_PROJECT_PATH=~/cloude-projects
CLOUDE_LOG_DIR=./logs
```

---

## 3. Preflight

```bash
bash scripts/preflight-bind-ip.sh
```

Why this exists: Docker Desktop's vpnkit silently accepts a stale `CLOUDE_BIND_IP` when you've switched networks (coffee shop → home, VPN up → VPN down). The container starts, the healthcheck goes green, and nothing answers on `:8000` because vpnkit never published the port. The preflight script greps `ifconfig` for your configured IP and fails loud if it's not assigned to a live interface.

Exit codes:

| Code | Meaning |
|---|---|
| `0` | IP is live, loopback, wildcard, or unset (falls back to 127.0.0.1) |
| `1` | IP set but not assigned to any interface — fix `.env` before `up` |

Re-run it any time you change networks.

---

## 4. Build and run

```bash
UID=$(id -u) GID=$(id -g) docker compose build
docker compose up -d
docker compose logs -f
```

The `UID=$(id -u) GID=$(id -g)` prefix is **required on the build line**, not optional. It passes your host user's UID/GID as build-args so the `cloude` user inside the container matches your host user. Without it, bind-mounted volumes (`~/.claude`, project dir, logs) end up owned by UID 1000 and your host user can't read them.

Wait for `Application startup complete` in the logs, then open `http://<CLOUDE_BIND_IP>:8000` (or `http://localhost:8000` if unset).

---

## 5. Volume layout

| Host | Container | Purpose |
|---|---|---|
| `~/.claude` | `/home/cloude/.claude` | Claude CLI auth, MCP config |
| `${CLOUDE_PROJECT_PATH}` | `/workspace` | Project dir Claude operates on |
| `./config.json` | `/app/config.json:ro` | Runtime config (projects, slash commands) |
| `./.env` | `/app/.env:ro` | Secrets |
| `${CLOUDE_LOG_DIR}` | `/var/log/cloude` | Session metadata + refresh token DB |

The `:ro` bind on `.env` and `config.json` is deliberate — the container should never rewrite them. If you edit either, `docker compose restart cloude` to pick up changes.

---

## 6. Authentication options for Mode 2

| Method | Mode 2 support | Notes |
|---|---|---|
| Anthropic API key (`ANTHROPIC_API_KEY`) | ✅ Yes | Set in `.env`, works immediately |
| Claude Pro / Max OAuth | ❌ **NOT SUPPORTED** | Requires macOS Keychain, unreachable from Linux container. Use Mode 1 |
| Claude Console org key | ✅ Yes | Same as API key path |

If you try to run `claude` inside the container with no `ANTHROPIC_API_KEY` and no OAuth, it will prompt for login and fail. This is expected — set the API key or switch to Mode 1.

---

## 7. MCP compatibility

| MCP category | Mode 2 | Example servers |
|---|---|---|
| Network-only (HTTP / stdio over network) | ✅ Works | Gmail, GDrive, Postgres, n8n, custom HTTP |
| Pure-stdio, portable binary | ✅ Works | filesystem MCP scoped to `/workspace`, git, generic stdio servers |
| macOS-native | ❌ Does not work | Shortcuts, AppleScript, Calendar, Reminders, Messages, Finder |
| Keychain-backed | ❌ Does not work | Anything calling `security find-generic-password` or Keychain APIs |

**If you rely on any red-X category, run Mode 1.** The container has no bridge to macOS system services — there is no workaround short of the hybrid mode we explicitly cut.

---

## 8. Networking

The compose file publishes port 8000 on `${CLOUDE_BIND_IP:-127.0.0.1}`. Key points:

- **Default is loopback-only.** An unset or misspelled `CLOUDE_BIND_IP` falls back to `127.0.0.1`, not `0.0.0.0`. This is deliberate — LAN exposure is opt-in.
- **LAN exposure:** set `CLOUDE_BIND_IP` to a specific host IP (e.g., `192.168.1.250`). Run the preflight script first.
- **macOS firewall:** System Settings → Network → Firewall must allow incoming connections on port 8000 for phone/tablet access.
- **Remote access:** The Docker path does not auto-provision a Cloudflare tunnel — that's a Mode 1 feature. For remote access with Mode 2, front the container with Tailscale (recommended), Cloudflare Tunnel (manual sidecar), or a reverse proxy of your choice.

---

## 9. Updating

```bash
cd cloudecode
git pull
UID=$(id -u) GID=$(id -g) docker compose build
docker compose up -d
```

The image rebuild picks up new Python deps and code changes. Your `.env`, `config.json`, `~/.claude`, and log volumes persist across rebuilds.

To force a clean slate without nuking config:

```bash
docker compose down
docker image rm cloude-code:local
UID=$(id -u) GID=$(id -g) docker compose build --no-cache
docker compose up -d
```

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Cannot connect to the Docker daemon` | Docker Desktop not running | Start Docker Desktop, wait for the whale icon to stop animating |
| `Bind for 0.0.0.0:8000 failed: port is already allocated` | Something else on port 8000 | `lsof -i :8000` on host, kill the squatter or change the host-side port in `docker-compose.yml` |
| Container healthy but `http://<ip>:8000` times out | Stale `CLOUDE_BIND_IP` (vpnkit silent failure) | Re-run `scripts/preflight-bind-ip.sh`, update `.env`, `docker compose up -d` |
| `Claude CLI: not authenticated` | Pro/Max OAuth attempt in Mode 2 | OAuth is not supported in Mode 2 — set `ANTHROPIC_API_KEY` in `.env` or switch to Mode 1 |
| `Permission denied` on `~/.claude/*` or `/workspace` | Container UID doesn't match host UID | Rebuild with `UID=$(id -u) GID=$(id -g) docker compose build` |
| TOTP code rejected | Host clock drift | `sudo sntp -sS time.apple.com` (macOS), container inherits host time |
| `ModuleNotFoundError` after `git pull` | New Python dep, stale image | Rebuild: `UID=$(id -u) GID=$(id -g) docker compose build` |
| `docker compose` logs stop at "Starting Claude CLI..." | Missing API key and no OAuth | Set `ANTHROPIC_API_KEY` in `.env`, `docker compose restart cloude` |

For anything else, `docker compose logs -f cloude` is your first call. The server logs structured JSON — pipe through `jq` if you want it pretty.
