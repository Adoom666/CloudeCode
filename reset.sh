#!/bin/bash
#
# Reset script for Cloude Code.
#
# Two modes, auto-detected:
#
#   launchd-managed - the server runs under a macOS LaunchAgent with
#     KeepAlive=true (label defaults to com.imc.cloude-code, override
#     with CLOUDE_LAUNCHD_LABEL). Under that setup, stop.sh killing the
#     server's process triggers an IMMEDIATE launchd respawn, and this
#     script's own start.sh then races that respawn for the same port -
#     one of the two loses and the reset either half-works or leaves two
#     processes fighting over the port. `launchctl kickstart -k` is the
#     correct primitive here: it asks launchd itself to restart its own
#     managed job, so there is exactly one respawn, not two.
#
#   unmanaged - no matching launchd job is loaded (e.g. running via
#     `./start.sh` directly, or on Linux/CI). Falls back to the original
#     stop.sh + sleep + start.sh flow.
#
# Inputs: none (reads CLOUDE_LAUNCHD_LABEL from the environment, optional).
# Output: exit 0 on a verified-up server on the configured port, exit 1
#   otherwise.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
source "${SCRIPT_DIR}/scripts/resolve-port.sh"

LAUNCHD_LABEL="${CLOUDE_LAUNCHD_LABEL:-com.imc.cloude-code}"

echo "=== Resetting Cloude Code Server ==="

# Resolve the configured port from .env up front (see
# scripts/resolve-port.sh for the three-outcome contract). A malformed
# PORT= must stop this script here, not surface later as a confusing
# "server did not come up" verdict against the wrong port.
PORT="$(resolve_port "${SCRIPT_DIR}")" || {
    echo "ERROR: could not determine port - see reason above. Fix PORT= in .env and re-run."
    exit 1
}

launchd_job_loaded() {
    # Description: True if a launchd job with $LAUNCHD_LABEL is loaded
    #   for the current console user. macOS-only (launchctl doesn't
    #   exist on Linux/CI, where this always returns false).
    command -v launchctl >/dev/null 2>&1 || return 1
    launchctl list 2>/dev/null | grep -q "$LAUNCHD_LABEL"
}

if launchd_job_loaded; then
    echo "Detected launchd-managed job '$LAUNCHD_LABEL' - using launchctl kickstart"
    echo "(stop.sh + start.sh would race the launchd respawn for port ${PORT})"

    UID_NUM=$(id -u)
    if launchctl kickstart -k "gui/${UID_NUM}/${LAUNCHD_LABEL}"; then
        echo "Waiting for launchd to bring the service back up..."
        sleep 3
        if lsof -ti:"${PORT}" > /dev/null 2>&1; then
            echo "Server is running on port ${PORT}"
            echo "Reset complete!"
            exit 0
        else
            echo "WARNING: launchctl kickstart returned success but port ${PORT} is not listening yet"
            exit 1
        fi
    else
        echo "ERROR: launchctl kickstart failed for gui/${UID_NUM}/${LAUNCHD_LABEL}"
        exit 1
    fi
fi

echo "No launchd job '$LAUNCHD_LABEL' found - using stop.sh + start.sh"

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
if lsof -ti:"${PORT}" > /dev/null 2>&1; then
    echo "Server is running on port ${PORT}"
    echo "Reset complete!"
    exit 0
else
    echo "WARNING: Server may not have started properly"
    exit 1
fi
