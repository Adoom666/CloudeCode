#!/bin/bash

# Stop script for Claude Code Controller

echo "Stopping FastAPI server..."

# Find processes using port 8000
PIDS=$(lsof -ti:8000)

if [ -z "$PIDS" ]; then
    echo "No processes found on port 8000"
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
REMAINING=$(lsof -ti:8000)
if [ ! -z "$REMAINING" ]; then
    echo "Force killing remaining processes: $REMAINING"
    for PID in $REMAINING; do
        kill -9 $PID 2>/dev/null
    done
fi

# Verify port is free
if lsof -ti:8000 > /dev/null 2>&1; then
    echo "ERROR: Port 8000 is still in use"
    exit 1
else
    echo "Port 8000 is now free"
fi
