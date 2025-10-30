#!/bin/bash

# Setup script for Cloude Code
# Checks and configures all required authentication

set -e

echo "☁️ Cloude Code - Setup"
echo "========================================"
echo ""

# Color codes
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Track if any setup is needed
NEEDS_SETUP=false

# Check cloudflared
echo ""
echo "Checking cloudflared..."
if command -v cloudflared &> /dev/null; then
    echo -e "${GREEN}✓${NC} cloudflared is installed"

    # Check authentication
    if [ -f ~/.cloudflared/cert.pem ]; then
        echo -e "${GREEN}✓${NC} cloudflared is authenticated"
    else
        echo -e "${YELLOW}!${NC} cloudflared is not authenticated"
        echo "  Run: cloudflared login"
        NEEDS_SETUP=true
    fi
else
    echo -e "${RED}✗${NC} cloudflared is not installed"
    echo "  Install with: brew install cloudflared"
    NEEDS_SETUP=true
fi

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

if [ -n "$CLAUDE_PATH" ]; then
    echo "  Detected path: $CLAUDE_PATH"
    echo "  To override, set CLAUDE_CLI_PATH in your .env file"
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

# Check directories (using values from .env if it exists)
echo ""
echo "Checking directories..."

# Load directory paths from .env if available
LOG_DIR="/tmp/claude-code-logs"  # default
PROJECTS_DIR=~/claude-projects    # default

if [ -f ".env" ]; then
    if grep -q "LOG_DIRECTORY=" .env; then
        LOG_DIR=$(grep "LOG_DIRECTORY=" .env | cut -d'=' -f2)
        LOG_DIR=$(eval echo "$LOG_DIR")  # Expand ~ and variables
    fi
    if grep -q "DEFAULT_WORKING_DIR=" .env; then
        PROJECTS_DIR=$(grep "DEFAULT_WORKING_DIR=" .env | cut -d'=' -f2)
        PROJECTS_DIR=$(eval echo "$PROJECTS_DIR")  # Expand ~ and variables
    fi
fi

if [ -d "$LOG_DIR" ]; then
    echo -e "${GREEN}✓${NC} Log directory exists: $LOG_DIR"
else
    echo -e "${YELLOW}!${NC} Creating log directory: $LOG_DIR"
    mkdir -p "$LOG_DIR"
    echo -e "${GREEN}✓${NC} Log directory created"
fi

if [ -d "$PROJECTS_DIR" ]; then
    echo -e "${GREEN}✓${NC} Projects directory exists: $PROJECTS_DIR"
else
    echo -e "${YELLOW}!${NC} Creating projects directory: $PROJECTS_DIR"
    mkdir -p "$PROJECTS_DIR"
    echo -e "${GREEN}✓${NC} Projects directory created"
fi

# Check .env file
echo ""
echo "Checking configuration..."
if [ -f ".env" ]; then
    echo -e "${GREEN}✓${NC} .env file exists"

    # Check for Cloudflare credentials
    if grep -q "CLOUDFLARE_API_TOKEN=" .env && grep -q "CLOUDFLARE_ZONE_ID=" .env; then
        TOKEN=$(grep "CLOUDFLARE_API_TOKEN=" .env | cut -d'=' -f2)
        ZONE=$(grep "CLOUDFLARE_ZONE_ID=" .env | cut -d'=' -f2)

        if [ -z "$TOKEN" ] || [ -z "$ZONE" ]; then
            echo -e "${YELLOW}!${NC} Cloudflare credentials not configured in .env"
            echo "  Add CLOUDFLARE_API_TOKEN and CLOUDFLARE_ZONE_ID to .env"
            NEEDS_SETUP=true
        else
            echo -e "${GREEN}✓${NC} Cloudflare credentials configured"
        fi
    else
        echo -e "${YELLOW}!${NC} Cloudflare credentials not found in .env"
        NEEDS_SETUP=true
    fi

    # Check for authentication secrets
    echo ""
    echo "Checking authentication secrets..."
    if grep -q "TOTP_SECRET=" .env && grep -q "JWT_SECRET=" .env; then
        TOTP=$(grep "TOTP_SECRET=" .env | cut -d'=' -f2)
        JWT=$(grep "JWT_SECRET=" .env | cut -d'=' -f2)

        if [ -z "$TOTP" ] || [ -z "$JWT" ]; then
            echo -e "${YELLOW}!${NC} Authentication secrets not configured in .env"
            echo "  Run: ./setup_auth.py"
            NEEDS_SETUP=true
        else
            echo -e "${GREEN}✓${NC} Authentication secrets configured"
        fi
    else
        echo -e "${YELLOW}!${NC} Authentication secrets not found in .env"
        echo "  Run: ./setup_auth.py"
        NEEDS_SETUP=true
    fi
else
    echo -e "${YELLOW}!${NC} .env file not found"
    echo "  Copying from .env.example..."
    cp .env.example .env
    echo -e "${GREEN}✓${NC} .env file created"
    echo -e "${YELLOW}!${NC} Please edit .env and add your Cloudflare credentials"
    NEEDS_SETUP=true
fi

# Summary
echo ""
echo "========================================"
if [ "$NEEDS_SETUP" = true ]; then
    echo -e "${YELLOW}⚠ Setup incomplete${NC}"
    echo ""
    echo "Next steps:"
    echo "1. Install missing dependencies (see above)"
    echo "2. Run: cloudflared login"
    echo "3. Edit .env and add:"
    echo "   - CLOUDFLARE_API_TOKEN (from Cloudflare dashboard)"
    echo "   - CLOUDFLARE_ZONE_ID (from Cloudflare dashboard)"
    echo "4. Run this script again to verify"
    echo ""
    echo "To get Cloudflare credentials:"
    echo "  1. Go to: https://dash.cloudflare.com/profile/api-tokens"
    echo "  2. Create token with 'Zone.DNS' edit permissions"
    echo "  3. Get Zone ID from your domain's overview page"
    exit 1
else
    echo -e "${GREEN}✓ Setup complete!${NC}"
    echo ""
    echo "You can now start the server:"
    echo "  ./start.sh"
    echo ""
    echo "Or run manually:"
    echo "  source venv/bin/activate"
    echo "  python3 -m src.main"
fi
