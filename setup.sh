#!/bin/bash

# Setup script for Cloude Code
# Interactive setup that prompts for all required configuration

set -e

echo "☁️ Cloude Code - Setup"
echo "========================================"
echo ""

# Color codes
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Track if any setup is needed
NEEDS_SETUP=false

# Plan v3.2 demolished the Cloudflare tunnel system. What stood here was a
# hard gate: no cloudflared binary meant `exit 1`, so setup refused to run
# at all on a machine with no use for a feature this app no longer has. It
# also ran `cloudflared login`, an interactive browser flow, to authorise a
# tunnel nothing would ever create.

# Check claude
echo ""
echo "Checking claude..."
CLAUDE_PATH=""
if command -v claude &> /dev/null; then
    CLAUDE_PATH=$(command -v claude)
    echo -e "${GREEN}✓${NC} claude is installed at: $CLAUDE_PATH"
elif [ -f ~/.claude/local/claude ]; then
    CLAUDE_PATH="$HOME/.claude/local/claude"
    echo -e "${GREEN}✓${NC} claude is installed at: $CLAUDE_PATH"
else
    echo -e "${YELLOW}!${NC} claude is not installed (optional)"
    echo "  Install from: https://claude.com/download"
fi

# Check tmux (required for session persistence across restarts)
echo ""
echo "Checking tmux..."
if command -v tmux &> /dev/null; then
    TMUX_VERSION=$(tmux -V | awk '{print $2}')
    echo -e "${GREEN}✓${NC} tmux $TMUX_VERSION is installed"
else
    echo -e "${YELLOW}⚠${NC}  tmux not found — install with 'brew install tmux' or session persistence will be disabled"
    echo "   Cloude Code will fall back to the PTY backend (sessions die on server restart)."
    echo "   To force PTY mode and silence this warning, set \"session.backend\": \"pty\" in config.json."
    # Intentionally NOT setting NEEDS_SETUP=true — tmux is optional, PTY fallback still works.
fi

# Check Python
echo ""
echo "Checking Python..."
if command -v python3 &> /dev/null; then
    PYTHON_VERSION=$(python3 --version | cut -d' ' -f2)
    echo -e "${GREEN}✓${NC} Python $PYTHON_VERSION is installed"
else
    echo -e "${RED}✗${NC} Python 3 is not installed"
    NEEDS_SETUP=true
    exit 1
fi

# Check virtual environment
echo ""
echo "Checking Python virtual environment..."
if [ -d "venv" ]; then
    echo -e "${GREEN}✓${NC} Virtual environment exists"
else
    echo -e "${YELLOW}!${NC} Virtual environment not found"
    echo "  Creating virtual environment..."
    python3 -m venv venv
    echo -e "${GREEN}✓${NC} Virtual environment created"
fi

# Check dependencies
echo ""
echo "Checking Python dependencies..."
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
    if python3 -c "import fastapi" 2>/dev/null; then
        echo -e "${GREEN}✓${NC} Python dependencies installed"
    else
        echo -e "${YELLOW}!${NC} Installing Python dependencies..."
        pip install -r requirements.txt
        echo -e "${GREEN}✓${NC} Dependencies installed"
    fi
    deactivate
fi

# Function to prompt for input with default value
prompt_with_default() {
    local prompt_text=$1
    local default_value=$2
    local var_name=$3

    if [ -n "$default_value" ]; then
        echo -e "${BLUE}?${NC} $prompt_text [${YELLOW}$default_value${NC}]"
    else
        echo -e "${BLUE}?${NC} $prompt_text"
    fi
    read -r input

    if [ -z "$input" ] && [ -n "$default_value" ]; then
        eval "$var_name='$default_value'"
    else
        eval "$var_name='$input'"
    fi
}

# Check if .env exists and has all required values
ENV_EXISTS=false
ENV_COMPLETE=false

if [ -f ".env" ]; then
    ENV_EXISTS=true

    # Check if all required values are present.
    #
    # The three CLOUDFLARE_* checks that used to be here were removed: the
    # Cloudflare tunnel system went away in Plan v3.2 and .env.example has
    # not carried those keys since, so no install could satisfy them and
    # ENV_COMPLETE was permanently false. That dragged an already-configured
    # user back through setup_auth.py further down this script - the same
    # dead check, and the same consequence, as the one in
    # macOS/server-manager.js checkConfiguration().
    if grep -q "^TOTP_SECRET=.\+" .env && \
       grep -q "^JWT_SECRET=.\+" .env; then
        ENV_COMPLETE=true
    fi
fi

if [ "$ENV_COMPLETE" = true ]; then
    echo ""
    echo -e "${GREEN}✓${NC} Configuration already complete"

    # Still check/create directories
    LOG_DIR="/tmp/cloude-code-logs"  # default
    PROJECTS_DIR=~/cloude-projects    # default

    if grep -q "LOG_DIRECTORY=" .env; then
        LOG_DIR=$(grep "LOG_DIRECTORY=" .env | cut -d'=' -f2)
        LOG_DIR=$(eval echo "$LOG_DIR")  # Expand ~ and variables
    fi
    if grep -q "DEFAULT_WORKING_DIR=" .env; then
        PROJECTS_DIR=$(grep "DEFAULT_WORKING_DIR=" .env | cut -d'=' -f2)
        PROJECTS_DIR=$(eval echo "$PROJECTS_DIR")  # Expand ~ and variables
    fi

    mkdir -p "$LOG_DIR"
    mkdir -p "$PROJECTS_DIR"

    echo ""
    echo "========================================"
    echo -e "${GREEN}✓ Setup complete!${NC}"
    echo ""
    echo "You can now start the server:"
    echo "  ./start.sh"
    echo ""
    echo "Or run manually:"
    echo "  source venv/bin/activate"
    echo "  python3 -m src.main"
    exit 0
fi

# Interactive configuration
echo ""
echo "========================================"
echo "Interactive Configuration Setup"
echo "========================================"
echo ""
echo "Let's configure Cloude Code. Nothing external is required - this writes"
echo "local paths and an authentication secret into .env."
echo ""
echo "Press Enter to continue..."
read -r

# Copy .env.example if .env doesn't exist
if [ "$ENV_EXISTS" = false ]; then
    cp .env.example .env
    echo -e "${GREEN}✓${NC} Created .env from .env.example"
    echo ""
fi

# The Cloudflare block that stood here asked for a domain, an API token, a
# zone ID and a tunnel name. All four were already dead: .env.example carries
# no CLOUDFLARE_* keys, so the sed lines that were supposed to store them
# matched nothing and wrote nothing. Setup collected an API token from the
# user and silently discarded it.

# Prompt for optional settings
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Optional Settings"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Get current optional values
CURRENT_WORKING_DIR=""
CURRENT_LOG_DIR=""

if [ -f ".env" ]; then
    CURRENT_WORKING_DIR=$(grep "DEFAULT_WORKING_DIR=" .env | cut -d'=' -f2 || echo "")
    CURRENT_LOG_DIR=$(grep "LOG_DIRECTORY=" .env | cut -d'=' -f2 || echo "")
fi

DEFAULT_WORKING_DIR="${CURRENT_WORKING_DIR:-~/cloude-projects}"
DEFAULT_LOG_DIR="${CURRENT_LOG_DIR:-/tmp/cloude-code-logs}"

prompt_with_default "Claude projects directory:" "$DEFAULT_WORKING_DIR" WORKING_DIR
prompt_with_default "Log directory:" "$DEFAULT_LOG_DIR" LOG_DIR

# Claude CLI path
if [ -n "$CLAUDE_PATH" ]; then
    prompt_with_default "Claude CLI path:" "$CLAUDE_PATH" CLI_PATH
else
    prompt_with_default "Claude CLI path (leave empty for auto-detect):" "" CLI_PATH
fi

# Write values to .env
echo ""
echo "Writing configuration to .env..."

# Read .env.example as template
cp .env.example .env.tmp

# Update values
sed -i '' "s|DEFAULT_WORKING_DIR=.*|DEFAULT_WORKING_DIR=$WORKING_DIR|" .env.tmp
sed -i '' "s|LOG_DIRECTORY=.*|LOG_DIRECTORY=$LOG_DIR|" .env.tmp

if [ -n "$CLI_PATH" ]; then
    sed -i '' "s|CLAUDE_CLI_PATH=.*|CLAUDE_CLI_PATH=$CLI_PATH|" .env.tmp
fi

mv .env.tmp .env
echo -e "${GREEN}✓${NC} Configuration written to .env"

# Create directories
echo ""
echo "Creating directories..."
EXPANDED_LOG_DIR=$(eval echo "$LOG_DIR")
EXPANDED_WORKING_DIR=$(eval echo "$WORKING_DIR")

mkdir -p "$EXPANDED_LOG_DIR"
mkdir -p "$EXPANDED_WORKING_DIR"

echo -e "${GREEN}✓${NC} Created log directory: $EXPANDED_LOG_DIR"
echo -e "${GREEN}✓${NC} Created projects directory: $EXPANDED_WORKING_DIR"

# Run setup_auth.py to generate secrets
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Generating Authentication Secrets"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Running setup_auth.py to generate TOTP and JWT secrets..."

source venv/bin/activate
python3 setup_auth.py
deactivate

echo ""
echo -e "${GREEN}✓${NC} Authentication setup complete"
echo ""
echo "A QR code has been generated (totp-qr.png)"
echo "Scan this with your authenticator app (Google Authenticator, Authy, etc.)"

# Summary
echo ""
echo "========================================"
echo -e "${GREEN}✓ Setup complete!${NC}"
echo "========================================"
echo ""
echo "Configuration summary:"
echo "  Projects: $EXPANDED_WORKING_DIR"
echo "  Logs: $EXPANDED_LOG_DIR"
echo ""
echo "Next steps:"
echo "  1. Scan the QR code in totp-qr.png with your authenticator app"
echo "  2. Start the server: ./start.sh"
echo ""
echo "Or run manually:"
echo "  source venv/bin/activate"
echo "  python3 -m src.main"
echo ""
