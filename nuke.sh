#!/bin/bash

# Nuke script for Cloude Code.
# Completely removes local configuration, venv, and state.
#
# Plan v3.2: the Cloudflare tunnel system was demolished. This script no
# longer touches Cloudflare resources (tunnels, DNS records) or the
# `cloudflared` binary state — there's nothing to tear down on Cloudflare.

set -e

echo "☁️💥 Cloude Code - Nuke it from Orbit!"
echo "========================================"
echo ""

# Color codes
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Check if --skip-confirm flag is passed (for macOS app usage)
SKIP_CONFIRM=false
if [ "$1" = "--skip-confirm" ]; then
    SKIP_CONFIRM=true
    echo "Running in non-interactive mode (confirmation already provided)"
fi

# Confirmation (only if not skipped)
if [ "$SKIP_CONFIRM" = "false" ]; then
    echo -e "${RED}⚠️  WARNING ⚠️${NC}"
    echo ""
    echo "This will completely remove ALL Cloude Code configuration and setup:"
    echo ""
    echo "  ✗ All local configuration files (.env, config.json, etc.)"
    echo "  ✗ Python virtual environment"
    echo "  ✗ All logs and temporary files"
    echo "  ✗ macOS app settings and LaunchAgent"
    echo ""
    echo -e "${YELLOW}You will need to run setup.sh again to use Cloude Code.${NC}"
    echo ""
    read -p "Are you ABSOLUTELY SURE you want to continue? (type 'NUKE' to confirm): " CONFIRM

    if [ "$CONFIRM" != "NUKE" ]; then
        echo ""
        echo "Aborted. No changes made."
        exit 0
    fi
fi

echo ""
echo "========================================"
echo "Starting cleanup process..."
echo "========================================"
echo ""

# Track what we've cleaned up
CLEANUP_LOG=()

# Function to log cleanup actions
log_cleanup() {
    local message=$1
    CLEANUP_LOG+=("$message")
    echo -e "${GREEN}✓${NC} $message"
}

# Function to log skipped items
log_skip() {
    local message=$1
    echo -e "${BLUE}○${NC} $message (not found, skipping)"
}

# 1. Stop running processes
echo "Stopping running processes..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Stop server (find by port 8000 or python process)
SERVER_PID=$(lsof -ti:8000 2>/dev/null || echo "")
if [ -n "$SERVER_PID" ]; then
    kill -9 $SERVER_PID 2>/dev/null || true
    log_cleanup "Stopped server process (PID: $SERVER_PID)"
else
    log_skip "Server process not running"
fi

# Stop macOS menubar app
MENUBAR_PIDS=$(pgrep -f "Cloude Code" || echo "")
if [ -n "$MENUBAR_PIDS" ]; then
    echo "$MENUBAR_PIDS" | xargs kill -9 2>/dev/null || true
    log_cleanup "Stopped macOS menubar app"
else
    log_skip "macOS menubar app not running"
fi

echo ""

# Source .env so subsequent steps can resolve LOG_DIRECTORY / DEFAULT_WORKING_DIR.
if [ -f ".env" ]; then
    source .env
fi

# 3. Remove local files
echo "Removing local files..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Project root files (use script directory)
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$SCRIPT_DIR"
cd "$PROJECT_ROOT"

FILES_TO_DELETE=(
    ".env"
    "config.json"
    "totp-qr.png"
    "session_metadata.json"
    ".env.tmp"
)

for file in "${FILES_TO_DELETE[@]}"; do
    if [ -f "$file" ]; then
        rm -f "$file"
        log_cleanup "Removed: $file"
    else
        log_skip "File: $file"
    fi
done

# Remove venv
if [ -d "venv" ]; then
    rm -rf "venv"
    log_cleanup "Removed: venv/"
else
    log_skip "Directory: venv/"
fi

echo ""

# 4. Remove directories
echo "Removing directories..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Get directory paths from .env if it existed (we already sourced it above)
LOG_DIR="${LOG_DIRECTORY:-/tmp/cloude-code-logs}"
PROJECTS_DIR="${DEFAULT_WORKING_DIR:-~/cloude-projects}"

# Expand ~ and variables
LOG_DIR=$(eval echo "$LOG_DIR")
PROJECTS_DIR=$(eval echo "$PROJECTS_DIR")

if [ -d "$LOG_DIR" ]; then
    rm -rf "$LOG_DIR"
    log_cleanup "Removed: $LOG_DIR"
else
    log_skip "Directory: $LOG_DIR"
fi

if [ -d "$PROJECTS_DIR" ]; then
    rm -rf "$PROJECTS_DIR"
    log_cleanup "Removed: $PROJECTS_DIR"
else
    log_skip "Directory: $PROJECTS_DIR"
fi

# Remove /tmp logs
TMP_LOGS=(
    "/tmp/cloudecode-server.log"
    "/tmp/cloudecode-menubar.log"
    "/tmp/cloudecode-menubar-error.log"
    "/tmp/electron-test.log"
)

for log_file in "${TMP_LOGS[@]}"; do
    if [ -f "$log_file" ]; then
        rm -f "$log_file"
        log_cleanup "Removed: $log_file"
    else
        log_skip "File: $log_file"
    fi
done

# Remove /tmp/cloude-app-extract
if [ -d "/tmp/cloude-app-extract" ]; then
    rm -rf "/tmp/cloude-app-extract"
    log_cleanup "Removed: /tmp/cloude-app-extract"
else
    log_skip "Directory: /tmp/cloude-app-extract"
fi

echo ""

# 6. Remove macOS LaunchAgent
echo "Removing macOS LaunchAgent..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

LAUNCH_AGENT="$HOME/Library/LaunchAgents/com.cloudecode.menubar.plist"
if [ -f "$LAUNCH_AGENT" ]; then
    # Unload first
    launchctl unload "$LAUNCH_AGENT" 2>/dev/null || true
    rm -f "$LAUNCH_AGENT"
    log_cleanup "Removed: $LAUNCH_AGENT"
else
    log_skip "LaunchAgent: com.cloudecode.menubar.plist"
fi

echo ""

# 7. Remove macOS App Support files
echo "Removing macOS App Support files..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Remove both possible app support directories
APP_SUPPORT_DIRS=(
    "$HOME/Library/Application Support/Cloude Code"
    "$HOME/Library/Application Support/cloude-code-menubar"
)

for APP_SUPPORT in "${APP_SUPPORT_DIRS[@]}"; do
    if [ -d "$APP_SUPPORT" ]; then
        rm -rf "$APP_SUPPORT"
        log_cleanup "Removed: $APP_SUPPORT"
    else
        log_skip "Directory: $APP_SUPPORT"
    fi
done

echo ""
echo "========================================"
echo -e "${GREEN}✓ Cleanup Complete!${NC}"
echo "========================================"
echo ""
echo "Summary of removed items:"
echo ""

# Display cleanup log
for item in "${CLEANUP_LOG[@]}"; do
    echo "  ✓ $item"
done

echo ""
echo -e "${YELLOW}Your system has been reset to a fresh state.${NC}"
echo ""
echo "To set up Cloude Code again, run:"
echo "  ./setup.sh"
echo ""
