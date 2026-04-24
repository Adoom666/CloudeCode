# Cloude Code - Complete Architecture Analysis

## What Is This App?

**Cloude Code** is a remote control platform for Claude Code CLI sessions. It lets you code from anywhere (couch, phone, tablet) by exposing your local Claude Code session over the internet through Cloudflare tunnels.

**Core value prop:** Control Claude Code remotely via web terminal + auto-detect dev servers and create public tunnel URLs with zero config.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        REMOTE CLIENT                                │
│  (Browser on phone/tablet/laptop anywhere on internet)              │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                │ HTTPS (Cloudflare Tunnel)
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     CLOUDFLARE EDGE                                 │
│  (Quick tunnels: random URL, Named tunnels: custom domain)         │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                │ cloudflared process
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     LOCAL MACHINE (macOS)                           │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  Electron Menu Bar App (macOS/main.js)                       │  │
│  │  - Tray icon with status indicators                          │  │
│  │  - Manages Python server lifecycle                           │  │
│  │  - Auto-launch via LaunchAgent                               │  │
│  │  - Health polling every 5s                                   │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                │                                    │
│                                │ spawns/manages                     │
│                                ▼                                    │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  FastAPI Backend (src/main.py - port 8000)                   │  │
│  │                                                               │  │
│  │  REST API Endpoints:                                          │  │
│  │  - POST /api/v1/auth/verify (TOTP → JWT)                     │  │
│  │  - POST/GET/DELETE /api/v1/sessions                          │  │
│  │  - GET/POST/DELETE /api/v1/tunnels                           │  │
│  │  - GET /health (no auth, for menu bar polling)               │  │
│  │                                                               │  │
│  │  WebSocket Endpoint:                                          │  │
│  │  - /ws/terminal (real-time PTY streaming)                    │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                │                                    │
│                                │ manages                            │
│                                ▼                                    │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  SessionManager + PTY Session                                 │  │
│  │  - Spawns bash shell → runs Claude CLI                       │  │
│  │  - Bidirectional I/O via pseudo-terminal                     │  │
│  │  - Persists session metadata (survives restarts)             │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                │                                    │
│                                │ monitors output                    │
│                                ▼                                    │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  LogMonitor + AutoTunnelOrchestrator                         │  │
│  │  - Watches for "localhost:PORT" patterns                     │  │
│  │  - Auto-creates Cloudflare tunnel when detected              │  │
│  │  - Broadcasts tunnel_created event to all clients            │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Client-Server Communication Deep Dive

### 1. Authentication Flow

```
TOTP (Time-based One-Time Password)
       │
       ▼
POST /api/v1/auth/verify {code: "123456"}
       │
       ▼
Server validates with pyotp (±1 window for clock drift)
       │
       ▼
Returns JWT token {token: "...", expires_in: 1800}
       │
       ▼
Stored in localStorage['claude_tunnel_token']
       │
       ▼
All subsequent requests: Authorization: Bearer {token}
```

**Auth Files:**
- `/src/api/auth.py` - TOTP verification, JWT creation (lines 108-159)
- `/client/js/auth.js` - TOTP input UI, token storage

### 2. Session Creation

```
POST /api/v1/sessions
{
  working_dir: "/path/to/project",
  auto_start_claude: true,
  copy_templates: true
}
       │
       ▼
SessionManager.create_session()
       │
       ├──> Spawns PTY (pseudo-terminal)
       ├──> Forks bash shell process
       ├──> Runs: claude --dangerously-skip-permissions
       │
       ▼
Returns: {id, pty_pid, working_dir, status}
```

**Session Files:**
- `/src/core/session_manager.py` - PTY lifecycle management (14.8K lines)
- `/src/utils/pty_session.py` - Low-level PTY handling (8.2K lines)
- `/src/api/routes.py:85-150` - Session REST endpoints

### 3. WebSocket Terminal Connection

```
WS /ws/terminal?token={jwt}
       │
       ▼
Token verified via verify_jwt_token()
       │
       ▼
ConnectionManager.connect(websocket)
       │
       ▼
Subscribes to 3 async queues:
├── pty_output_queue (terminal bytes)
├── tunnel_queue (tunnel events)
└── log_queue (system messages)
       │
       ▼
Spawns 4 concurrent async tasks:
├── receive_messages() - Client input → PTY
├── send_pty_output() - PTY output → Client
├── send_queue_messages(tunnel) - Tunnel events
└── send_queue_messages(log) - Log events
```

**WebSocket Protocol:**

| Direction | Format | Description |
|-----------|--------|-------------|
| Client → Server | Binary (UTF-8) | User keyboard input |
| Client → Server | JSON | `{type: "pty_resize", cols, rows}` |
| Client → Server | JSON | `{type: "ping"}` (keepalive) |
| Server → Client | Binary | Raw PTY output bytes |
| Server → Client | JSON | `{type: "tunnel_created", tunnel}` |
| Server → Client | JSON | `{type: "log", content}` |
| Server → Client | JSON | `{type: "pong"}` |
| Server → Client | JSON | `{type: "error", message}` |

**WebSocket Files:**
- `/src/api/websocket.py` - Server-side handler (lines 72-298)
- `/client/js/terminal.js` - Client WebSocket + xterm.js (lines 391-558)

### 4. Auto-Tunnel Detection

```
Terminal output: "Server running on localhost:3000"
       │
       ▼
LogMonitor.detect_patterns()
  Pattern: "localhost:PORT" or "Listening on port"
       │
       ▼
AutoTunnelOrchestrator.on_port_detected(3000)
  - Checks if tunnel already exists for port
  - Prevents duplicates
       │
       ▼
HybridTunnelManager.create_tunnel(3000)
  ├── Quick Tunnel: `cloudflared tunnel --url http://localhost:3000`
  │   → Returns random URL: https://xxx.trycloudflare.com
  │
  └── Named Tunnel: Cloudflare API + DNS CNAME
      → Returns custom URL: https://3000.yourdomain.com
       │
       ▼
Broadcasts to all WebSocket clients:
{type: "tunnel_created", tunnel: {id, port, public_url, status}}
```

**Tunnel Files:**
- `/src/core/auto_tunnel.py` - Pattern detection → tunnel creation (5.1K lines)
- `/src/core/hybrid_tunnel_manager.py` - Abstract tunnel interface (5.8K lines)
- `/src/core/tunnel_manager.py` - Quick tunnels via cloudflared (8.8K lines)
- `/src/core/named_tunnel_manager.py` - Cloudflare API tunnels (16K lines)
- `/src/utils/patterns.py` - Port detection regex patterns (5.9K lines)

---

## Directory Structure

```
cloudecode/
├── src/                              # Python Backend
│   ├── main.py                       # FastAPI entry point
│   ├── config.py                     # Pydantic settings
│   ├── models.py                     # Data models, message types
│   ├── api/
│   │   ├── routes.py                 # REST endpoints
│   │   ├── websocket.py              # WebSocket terminal
│   │   └── auth.py                   # TOTP/JWT auth
│   ├── core/
│   │   ├── session_manager.py        # PTY session lifecycle
│   │   ├── log_monitor.py            # Terminal output monitoring
│   │   ├── auto_tunnel.py            # Auto-detect ports → create tunnels
│   │   ├── hybrid_tunnel_manager.py  # Tunnel abstraction
│   │   ├── tunnel_manager.py         # Quick tunnels (cloudflared)
│   │   ├── named_tunnel_manager.py   # Named tunnels (Cloudflare API)
│   │   └── cloudflare_api.py         # Cloudflare SDK wrapper
│   └── utils/
│       ├── pty_session.py            # Low-level PTY handling
│       ├── patterns.py               # Port/server regex patterns
│       └── template_manager.py       # Project template copying
│
├── client/                           # Web Frontend
│   ├── index.html                    # SPA shell
│   ├── css/styles.css                # Dark theme styles
│   └── js/
│       ├── api.js                    # REST + WebSocket client
│       ├── auth.js                   # TOTP auth UI
│       ├── terminal.js               # xterm.js integration
│       ├── launchpad.js              # Project picker
│       ├── dpad.js                   # Mobile controls
│       └── slash-commands.js         # Command palette
│
├── macOS/                            # Electron Menu Bar App
│   ├── main.js                       # Main process (634 lines)
│   ├── server-manager.js             # Python server lifecycle (795 lines)
│   ├── launchagent-installer.js      # Auto-launch config
│   └── assets/                       # Icons
│
├── .env                              # Secrets (not committed)
├── config.json                       # User config (projects, commands)
├── requirements.txt                  # Python deps
├── start.sh                          # Start server
└── setup_auth.py                     # Generate TOTP/JWT secrets
```

---

## Key Files by Function

### Entry Points
| File | Purpose |
|------|---------|
| `/src/main.py` | FastAPI app, route mounting, lifespan |
| `/client/index.html` | SPA shell, loads JS modules |
| `/macOS/main.js` | Electron main process, tray menu |

### Authentication
| File | Purpose |
|------|---------|
| `/src/api/auth.py:108-159` | TOTP verification, JWT creation |
| `/client/js/auth.js` | TOTP input UI, token storage |

### WebSocket Communication
| File | Purpose |
|------|---------|
| `/src/api/websocket.py:72-153` | WS endpoint, auth, message routing |
| `/src/api/websocket.py:155-227` | Receive client input → PTY |
| `/src/api/websocket.py:230-269` | Send PTY output → client |
| `/client/js/terminal.js:391-422` | WS connection setup |
| `/client/js/terminal.js:461-525` | Message handling, binary/JSON |

### PTY Session Management
| File | Purpose |
|------|---------|
| `/src/core/session_manager.py` | Session lifecycle, persistence |
| `/src/utils/pty_session.py` | Fork, exec, I/O handling |

### Auto-Tunneling
| File | Purpose |
|------|---------|
| `/src/core/auto_tunnel.py:93-99` | Port detected → create tunnel |
| `/src/utils/patterns.py` | Regex for localhost:PORT |
| `/src/core/hybrid_tunnel_manager.py` | Tunnel strategy abstraction |

### Menu Bar App
| File | Purpose |
|------|---------|
| `/macOS/main.js:187-535` | Tray menu, status display |
| `/macOS/server-manager.js:296-459` | Python server spawn/kill |
| `/macOS/server-manager.js:540-608` | Health polling |

---

## Configuration

### Environment Variables (.env)
```bash
# Server
HOST=0.0.0.0
PORT=8000
DEFAULT_WORKING_DIR=~/Documents/Cloude

# Auth (generated by setup_auth.py)
TOTP_SECRET=base32_secret
JWT_SECRET=random_hex

# Tunnels
TUNNEL_PROVIDER=cloudflared
AUTO_CREATE_TUNNELS=true
USE_NAMED_TUNNELS=false  # true = custom domains

# Cloudflare (for named tunnels)
CLOUDFLARE_API_TOKEN=xxx
CLOUDFLARE_ZONE_ID=xxx
CLOUDFLARE_DOMAIN=yourdomain.com
CLOUDFLARE_TUNNEL_ID=xxx
```

### config.json
```json
{
  "jwt_expiry_minutes": 30,
  "projects": [
    {"name": "my-project", "path": "~/code/my-project", "description": "..."}
  ],
  "common_slash_commands": ["/help", "/clear", "/config", ...]
}
```

---

## Security Model

1. **TOTP** - 2FA gate (Google Authenticator compatible)
2. **JWT** - 30 min tokens, HS256, required for all protected endpoints
3. **WebSocket Auth** - Token in query param, verified on connect
4. **CORS** - Configurable allowed origins
5. **Secrets** - .env not committed, cloudflare creds scoped

---

## Data Flow Summary

```
[Mobile Browser] ──HTTPS──> [Cloudflare Tunnel] ──> [localhost:8000]
                                                          │
                                    ┌─────────────────────┴─────────────────────┐
                                    │                                           │
                              [REST API]                               [WebSocket]
                                    │                                           │
                    ┌───────────────┼───────────────┐         ┌────────────────┴───────────────┐
                    │               │               │         │                                │
              /auth/verify    /sessions      /tunnels    Binary PTY I/O              JSON Events
                    │               │               │         │                                │
              TOTP→JWT      Create/Destroy   List/Create  Keyboard↔Terminal        tunnel_created
                            PTY Session      Cloudflare                             log, error
```

---

---

# Security Improvements from Happy Project Analysis

## Executive Summary

Analyzed three repos from slopus/happy ecosystem. They've built a zero-knowledge E2E encrypted architecture with some solid security patterns we should adopt. Also found gaps we should avoid.

---

## Security Features Worth Adopting

### 1. End-to-End Encryption (HIGH PRIORITY)

**What Happy Does:**
- Zero-knowledge architecture - server CANNOT decrypt user data
- All session data encrypted with per-session keys
- Uses NaCl/libsodium (XSalsa20-Poly1305) + AES-256-GCM
- Ephemeral keypairs per message for forward secrecy

**Current Cloude Code:**
- No encryption of session data
- Server sees all terminal output in plaintext
- Tunnel URLs visible to anyone with access

**Recommendation:**
```
Priority: MEDIUM (nice-to-have for V2)
Complexity: HIGH
```
For now, our TOTP + JWT + Cloudflare HTTPS is adequate. Full E2E would require client-side encryption of all terminal data before WebSocket transmission.

---

### 2. Hierarchical Key Derivation (MEDIUM PRIORITY)

**What Happy Does:**
```typescript
// HMAC-SHA512 key tree (BIP32-style)
const I = await hmac_sha512(
    new TextEncoder().encode(usage + ' Master Seed'),
    seed
);
return {
    key: I.slice(0, 32),
    chainCode: I.slice(32)
};
```
- Derive per-session, per-machine, per-artifact keys from master
- Domain separation via usage strings
- Can't derive one key from another without the path

**Current Cloude Code:**
- Single JWT_SECRET for all tokens
- Single TOTP_SECRET for auth

**Recommendation:**
```
Priority: LOW
Complexity: MEDIUM
```
Our current single-secret model is fine for a single-user app. Would matter more for multi-tenant.

---

### 3. WebSocket RPC Encryption (MEDIUM PRIORITY)

**What Happy Does:**
```typescript
// All RPC calls encrypted with session-specific keys
async sessionRPC(sessionId, method, params) {
    const sessionEncryption = this.encryption.getSessionEncryption(sessionId);
    const result = await this.socket.emitWithAck('rpc-call', {
        method: `${sessionId}:${method}`,
        params: await sessionEncryption.encryptRaw(params)
    });
    return await sessionEncryption.decryptRaw(result.result);
}
```

**Current Cloude Code:**
- WebSocket messages sent as plaintext JSON/binary
- Relies on TLS (HTTPS/WSS) for transport encryption

**Recommendation:**
```
Priority: LOW
Complexity: HIGH
```
Our Cloudflare tunnel provides TLS encryption. Additional layer-7 encryption is defense-in-depth but overkill for our use case.

---

### 4. Scoped Connection Types (HIGH PRIORITY) ⭐

**What Happy Does:**
```typescript
// Three connection types with different access levels
'user-scoped'      // All user data
'session-scoped'   // Single session only
'machine-scoped'   // Daemon/machine specific
```
WebSocket validates scope and restricts data access accordingly.

**Current Cloude Code:**
- Single connection type - full access once authenticated
- No granular scoping

**Recommendation:**
```
Priority: MEDIUM
Complexity: MEDIUM
Files to modify:
- /src/api/websocket.py (add clientType param)
- /src/models.py (add ConnectionScope enum)
- /client/js/api.js (pass scope in WS auth)
```
Could be useful if we add multi-session support later.

---

### 5. Rate Limiting & Brute Force Protection (HIGH PRIORITY) ⭐

**What Happy DOESN'T Do (and neither do we):**
- No rate limiting on auth endpoints
- No exponential backoff on failed auth
- Infinite polling without limits

**Current Cloude Code:**
- Same gaps - no rate limiting
- TOTP allows unlimited attempts

**Recommendation:**
```
Priority: HIGH
Complexity: LOW
Files to modify:
- /src/api/auth.py (add rate limiting decorator)
- /src/main.py (add slowapi or similar middleware)
```

**Implementation:**
```python
# Add to requirements.txt
slowapi==0.1.9

# In auth.py
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)

@router.post("/verify")
@limiter.limit("5/minute")  # 5 attempts per minute per IP
async def verify_totp(request: Request, ...):
```

---

### 6. Secure Token Storage (MEDIUM PRIORITY)

**What Happy Does:**
```typescript
// Native platforms use OS secure storage
if (Platform.OS === 'web') {
    localStorage.getItem(AUTH_KEY);  // Web fallback
} else {
    SecureStore.getItemAsync(AUTH_KEY);  // Keychain/Keystore
}
```

**Current Cloude Code:**
- Web only - uses localStorage (plaintext)

**Recommendation:**
```
Priority: LOW
Complexity: N/A (web-only app)
```
We're web-only, so localStorage is our only option. Could add sessionStorage for shorter-lived tokens.

---

### 7. User-Friendly Secret Key Format (LOW PRIORITY)

**What Happy Does:**
```typescript
// Base32 with typo correction (1Password style)
formatSecretKeyForBackup(secretKey) {
    // "XXXXX-XXXXX-XXXXX-XXXXX-XXXXX"
    // Auto-corrects: 0→O, 1→I, 8→B, 9→G
}
```

**Current Cloude Code:**
- TOTP secret is base32 (Google Authenticator compatible)
- No backup format for secret key

**Recommendation:**
```
Priority: LOW
Complexity: LOW
```
Nice UX improvement for manual secret entry, but we use QR codes.

---

### 8. Encrypted Error Responses (MEDIUM PRIORITY)

**What Happy Does:**
```typescript
// Errors encrypted - no info leakage
catch (error) {
    const errorResponse = { error: error.message };
    return encodeBase64(encrypt(key, errorResponse));
}
```

**Current Cloude Code:**
- Error messages sent as plaintext JSON
- Stack traces logged (but not sent to client)

**Recommendation:**
```
Priority: LOW
Complexity: MEDIUM
```
Our errors are generic enough. Main thing is ensuring no stack traces leak to client (we're good here).

---

### 9. CORS Hardening (HIGH PRIORITY) ⭐

**What Happy Does WRONG:**
```typescript
cors: {
    origin: "*",  // WIDE OPEN - BAD!
    credentials: true,
    allowedHeaders: ["*"]
}
```

**Current Cloude Code:**
```python
# In main.py
allow_origins=settings.allowed_origins  # Defaults to ["*"]
```

**Recommendation:**
```
Priority: HIGH
Complexity: LOW
Files to modify:
- /src/config.py (add explicit origin list)
- /src/main.py (use restrictive CORS)
- /.env.example (document ALLOWED_ORIGINS)
```

**Implementation:**
```python
# config.py
allowed_origins: list[str] = Field(
    default=["http://localhost:8000", "https://yourdomain.com"],
    description="Explicitly allowed CORS origins"
)

# main.py
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,  # NOT ["*"]
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)
```

---

### 10. Certificate Pinning (LOW PRIORITY)

**What Happy Doesn't Do:**
- No certificate pinning
- Standard TLS only

**Current Cloude Code:**
- Same - relies on Cloudflare/system TLS

**Recommendation:**
```
Priority: LOW
Complexity: HIGH
```
Overkill for our use case. Cloudflare handles TLS termination.

---

## Security Gaps in Happy We Should Avoid

| Issue | Happy Status | Our Status | Action |
|-------|--------------|------------|--------|
| Token cache memory leak | ❌ Unbounded Map | ✅ JWT expires | Keep our approach |
| CORS wide open | ❌ origin: "*" | ⚠️ Same | FIX THIS |
| No rate limiting | ❌ Missing | ❌ Missing | ADD THIS |
| 100MB body limit | ❌ Too high | ✅ Default (1MB) | Keep default |
| Debug logging default | ❌ Level: debug | ⚠️ Verbose | Review |
| Infinite WebSocket reconnect | ❌ Infinity | ✅ Max 5 attempts | Keep our approach |

---

## Recommended Security Roadmap

### Phase 1: Quick Wins (Do Now)
1. **Add rate limiting to auth endpoints** - 5 attempts/min per IP
2. **Harden CORS** - Explicit origin whitelist, not `*`
3. **Review logging** - Ensure no tokens/secrets in logs

### Phase 2: Improvements (V1.1)
4. **Add connection scoping** - session-scoped vs user-scoped
5. **Token expiry handling** - Client-side token refresh
6. **Brute force lockout** - Temporary ban after N failures

### Phase 3: Advanced (V2)
7. **End-to-end encryption** - Client-side encryption of terminal data
8. **Per-session encryption keys** - Key derivation from master secret
9. **Encrypted error responses** - Prevent info leakage

---

## Files to Modify for Phase 1

| File | Change |
|------|--------|
| `/src/main.py` | Add slowapi rate limiter middleware |
| `/src/api/auth.py` | Add rate limit decorator to verify endpoint |
| `/src/config.py` | Add explicit ALLOWED_ORIGINS list |
| `/.env.example` | Document ALLOWED_ORIGINS |
| `/requirements.txt` | Add `slowapi==0.1.9` |

---

## Summary

Happy has solid crypto (NaCl, AES-256-GCM, HMAC-SHA512 key derivation) but weak operational security (no rate limiting, CORS wide open). We should:

1. **Steal:** Rate limiting pattern (once they add it), connection scoping
2. **Skip:** Full E2E encryption (overkill for single-user), cert pinning
3. **Fix:** CORS (we have the same gap), add rate limiting
