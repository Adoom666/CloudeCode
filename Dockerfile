# syntax=docker/dockerfile:1.7
#
# Cloude Code Controller — production container image.
#
# Base: python:3.12-slim (Debian bookworm-slim). Multi-arch upstream, so we do
# NOT pin --platform here — Docker Desktop on Apple Silicon resolves arm64,
# Linux CI resolves amd64. Image is a single-stage build: the size win of a
# multi-stage for this workload is marginal (we need node + claude CLI in the
# runtime image, which dominates the layer budget), and single-stage keeps
# the failure surface obvious for a weekend MVP.
#
# UID/GID are build-args so `docker compose` can pass
# --build-arg UID=$(id -u) --build-arg GID=$(id -g)
# to keep bind-mounted volumes (e.g., ~/.claude, workspace dirs) owned by the
# host user inside the container — no chown dance at runtime, no root writes.
FROM python:3.12-slim

ARG UID=1000
ARG GID=1000

# ---------------------------------------------------------------------------
# 1. System dependencies.
#
# tmux            — session persistence backend (Item 1).
# git             — claude CLI + user workflows rely on it.
# ripgrep         — claude CLI uses `rg` for codebase search.
# jq              — shell tooling / setup scripts.
# curl            — NodeSource bootstrap + HEALTHCHECK probe.
# ca-certificates — TLS roots for curl / httpx / cloudflare / ntfy.
# libsecret-1-0   — runtime lib for credential-store keyring access. The
#                   claude CLI links against it for Keychain-style storage
#                   on Linux. Without it, npm install of @anthropic-ai/
#                   claude-code succeeds but OAuth login blows up at runtime.
# build-essential — gcc/make/headers for any npm postinstall that compiles
#                   native addons. Bigger than we'd like (~250MB) but needed
#                   for correctness; we accept the size hit per the plan.
#
# All in a single RUN so apt cache invalidation is atomic, followed by
# aggressive cleanup so the layer stays tight.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        tmux \
        git \
        ripgrep \
        jq \
        curl \
        ca-certificates \
        libsecret-1-0 \
        build-essential \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# 2. Node.js 22 via NodeSource.
#
# Separate RUN from system deps so the apt-list cleanup above stays effective
# (NodeSource drops its own list file under /etc/apt/sources.list.d which we
# wipe again after). `setup_22.x` writes the repo + imports the signing key,
# then a second apt-get install pulls nodejs.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# 3. Claude Code CLI.
#
# Unpinned per MVP plan — weekend scope accepts the "version drift" risk.
# Pin to a semver range before cutting a release. The -g install lands in
# /usr/lib/node_modules and shims into /usr/bin/claude, so the non-root
# `cloude` user we create later picks it up on PATH automatically.
RUN npm install -g @anthropic-ai/claude-code

# ---------------------------------------------------------------------------
# 4. Non-root user.
#
# Create before we pip-install so site-packages end up owned by root (read-only
# for the app user — safer default). `-m` gives /home/cloude for the claude
# CLI's keyring/cache files. `-s /bin/bash` for sane `docker exec -it` shells.
RUN groupadd -g ${GID} cloude \
 && useradd -u ${UID} -g ${GID} -m -s /bin/bash cloude

WORKDIR /app

# ---------------------------------------------------------------------------
# 5. Python dependencies.
#
# Copy requirements.txt FIRST and install separately so the pip layer caches
# across code-only rebuilds. --no-cache-dir keeps the wheel cache out of the
# image (saves ~50MB). pip runs as root so site-packages are owned by root
# (the `cloude` user can import but not mutate — defensive default).
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# ---------------------------------------------------------------------------
# 6. Application source.
#
# COPY the pieces the app actually needs at runtime. Anything not listed here
# (tests/, docs/, macOS/, venv/, setup.sh variants we don't need) is kept out
# by .dockerignore — see Item 11. `setup.sh` and `setup_auth.py` are kept
# because they're the documented first-run onboarding path inside the
# container (Item 14 docs will reference `docker exec -it ... setup_auth.py`).
COPY src/                 /app/src/
COPY client/              /app/client/
COPY config.example.json  /app/config.example.json
COPY setup_auth.py        /app/setup_auth.py
COPY setup.sh             /app/setup.sh

# Flip ownership in a single layer AFTER all COPYs so there's exactly one
# chown pass. Cheaper than `COPY --chown` repeated five times.
RUN chown -R cloude:cloude /app

USER cloude

# ---------------------------------------------------------------------------
# 7. Runtime environment.
#
# TERM — correction #10 in the plan. PTY code at src/utils/pty_session.py:76
# already sets TERM on its spawned children, but this default is for direct
# `docker exec -it` debugging sessions where no PTY wrapper is in play.
#
# PYTHONUNBUFFERED — stdout/stderr flush on write; critical for container log
# collectors (docker logs, k8s, whatever) to see structlog output in real time.
#
# PYTHONDONTWRITEBYTECODE — no .pyc clutter in the (read-only) app dir.
#
# CLOUDECODE_BIND_HOST — bind to 0.0.0.0 inside the container so the host
# port-publish (-p) can reach uvicorn. The app reads this via
# pydantic-settings (src/config.py).
ENV TERM=xterm-256color \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CLOUDECODE_BIND_HOST=0.0.0.0

EXPOSE 8000

# ---------------------------------------------------------------------------
# 8. Healthcheck.
#
# Hits the /health endpoint defined at src/main.py:351. 20s start_period gives
# lifespan tasks (tunnel init, refresh-store init, notification router) room
# to come up before the first probe. `curl -fsS` fails on HTTP >=400 and
# stays silent on success — no log noise per successful probe.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=20s \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# ---------------------------------------------------------------------------
# 9. Entrypoint.
#
# `python3 -m src.main` runs the __main__ block at src/main.py:361, which
# calls uvicorn.run with settings.host / settings.port. Host is driven by
# CLOUDECODE_BIND_HOST above; port defaults to 8000 in pydantic-settings.
CMD ["python3", "-m", "src.main"]
