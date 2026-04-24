#!/bin/bash

# Nuke script for Cloude Code
# Completely removes all configuration and setup from the system
# WARNING: This will delete the Cloudflare tunnel and all DNS records!

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
    echo "  ✗ Cloudflare tunnel will be DELETED from Cloudflare"
    echo "  ✗ All DNS records will be DELETED from Cloudflare"
    echo "  ✗ All local configuration files (.env, config.json, etc.)"
    echo "  ✗ Python virtual environment"
    echo "  ✗ All logs and temporary files"
    echo "  ✗ Cloudflared authentication and tunnel configs"
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

# Stop cloudflared tunnel
CLOUDFLARED_PIDS=$(pgrep -f "cloudflared tunnel" || echo "")
if [ -n "$CLOUDFLARED_PIDS" ]; then
    echo "$CLOUDFLARED_PIDS" | xargs kill -9 2>/dev/null || true
    log_cleanup "Stopped cloudflared tunnel processes"
else
    log_skip "Cloudflared tunnel not running"
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

# 2. Delete Cloudflare infrastructure
echo "Cleaning up Cloudflare infrastructure..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Load .env to get tunnel name and credentials
if [ -f ".env" ]; then
    source .env

    if [ -n "$CLOUDFLARE_TUNNEL_NAME" ] && command -v cloudflared &> /dev/null; then
        # Delete tunnel (this also removes DNS records associated with it)
        echo "Deleting Cloudflare tunnel: $CLOUDFLARE_TUNNEL_NAME"

        if cloudflared tunnel delete "$CLOUDFLARE_TUNNEL_NAME" -f 2>/dev/null; then
            log_cleanup "Deleted Cloudflare tunnel: $CLOUDFLARE_TUNNEL_NAME"
        else
            log_skip "Tunnel not found in Cloudflare (may have been deleted already)"
        fi

        # Delete DNS records manually if needed
        if [ -n "$CLOUDFLARE_API_TOKEN" ] && [ -n "$CLOUDFLARE_ZONE_ID" ] && [ -n "$CLOUDFLARE_DOMAIN" ]; then
            echo "Checking for DNS records to clean up..."

            # Get the tunnel ID if it exists
            TUNNEL_ID_FILE=$(find ~/.cloudflared -name "*.json" -not -name "cert.pem" 2>/dev/null | head -1)
            if [ -n "$TUNNEL_ID_FILE" ]; then
                TUNNEL_ID=$(basename "$TUNNEL_ID_FILE" .json)

                # Delete DNS records pointing to the tunnel
                # This uses curl to call Cloudflare API
                export AWS_PAGER=""  # Disable pager for AWS CLI compatibility

                # Get all DNS records
                DNS_RECORDS=$(curl -s -X GET "https://api.cloudflare.com/client/v4/zones/$CLOUDFLARE_ZONE_ID/dns_records" \
                    -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
                    -H "Content-Type: application/json")

                # Find records pointing to our tunnel
                RECORD_IDS=$(echo "$DNS_RECORDS" | grep -o '"id":"[^"]*"' | grep -B10 "$TUNNEL_ID" | grep '"id"' | cut -d'"' -f4 || echo "")

                if [ -n "$RECORD_IDS" ]; then
                    echo "$RECORD_IDS" | while read -r RECORD_ID; do
                        curl -s -X DELETE "https://api.cloudflare.com/client/v4/zones/$CLOUDFLARE_ZONE_ID/dns_records/$RECORD_ID" \
                            -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" >/dev/null
                    done
                    log_cleanup "Deleted DNS records from Cloudflare"
                else
                    log_skip "No DNS records found"
                fi
            fi
        fi
    else
        log_skip "Cloudflare tunnel name not found in .env or cloudflared not installed"
    fi
else
    log_skip ".env file not found, skipping Cloudflare cleanup"
fi

echo ""

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
    "/tmp/cloudflared-tunnel.log"
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

# 5. Remove cloudflared configs
echo "Removing cloudflared configuration..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Remove tunnel JSON files
TUNNEL_JSON_FILES=$(find ~/.cloudflared -name "*.json" -not -name "cert.pem" 2>/dev/null || echo "")
if [ -n "$TUNNEL_JSON_FILES" ]; then
    echo "$TUNNEL_JSON_FILES" | while read -r file; do
        rm -f "$file"
        log_cleanup "Removed: $file"
    done
else
    log_skip "Tunnel credential files"
fi

# Remove tunnel YAML files
TUNNEL_YAML_FILES=$(find ~/.cloudflared -name "*.yml" 2>/dev/null || echo "")
if [ -n "$TUNNEL_YAML_FILES" ]; then
    echo "$TUNNEL_YAML_FILES" | while read -r file; do
        rm -f "$file"
        log_cleanup "Removed: $file"
    done
else
    log_skip "Tunnel config files"
fi

# Remove cert.pem (Cloudflare auth certificate)
if [ -f ~/.cloudflared/cert.pem ]; then
    rm -f ~/.cloudflared/cert.pem
    log_cleanup "Removed: ~/.cloudflared/cert.pem"
else
    log_skip "File: ~/.cloudflared/cert.pem"
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
