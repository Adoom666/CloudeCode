#!/bin/bash

# Reset script for Cloude Code - Stop and Start

echo "=== Resetting Cloude Code Server ==="

# Stop the server
./stop.sh
if [ $? -ne 0 ]; then
    echo "ERROR: Failed to stop server"
    exit 1
fi

# Wait a moment before starting
echo "Waiting 2 seconds before restart..."
sleep 2

# Start the server in the background
echo "Starting server..."
./start.sh &

# Give it a moment to start
sleep 3

# Verify the server is up
if lsof -ti:8000 > /dev/null 2>&1; then
    echo "✓ Server is running on port 8000"
    echo "Reset complete!"
    exit 0
else
    echo "✗ WARNING: Server may not have started properly"
    exit 1
fi
