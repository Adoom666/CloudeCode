#!/bin/bash

# Stop script for Claude Code Controller

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
source "${SCRIPT_DIR}/scripts/resolve-port.sh"

echo "Stopping FastAPI server..."

# Resolve the configured port from .env (see scripts/resolve-port.sh for
# the three-outcome contract). A malformed PORT= must stop this script,
# not silently target the wrong port.
PORT="$(resolve_port "${SCRIPT_DIR}")" || {
    echo "ERROR: could not determine port - see reason above. Fix PORT= in .env and re-run."
    exit 1
}

# Find processes using the configured port
PIDS=$(lsof -ti:"${PORT}")

if [ -z "$PIDS" ]; then
    echo "No processes found on port ${PORT}"
    exit 0
fi

echo "Found processes: $PIDS"

# Kill processes gracefully
for PID in $PIDS; do
    echo "Killing process $PID..."
    kill $PID 2>/dev/null
done

# Wait a moment for graceful shutdown
sleep 2

# Force kill if still running
REMAINING=$(lsof -ti:"${PORT}")
if [ ! -z "$REMAINING" ]; then
    echo "Force killing remaining processes: $REMAINING"
    for PID in $REMAINING; do
        kill -9 $PID 2>/dev/null
    done
fi

# Verify port is free
if lsof -ti:"${PORT}" > /dev/null 2>&1; then
    echo "ERROR: Port ${PORT} is still in use"
    exit 1
else
    echo "Port ${PORT} is now free"
fi
