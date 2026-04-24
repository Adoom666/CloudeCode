#!/usr/bin/env bash
# Preflight check for docker compose up — verifies CLOUDE_BIND_IP is assigned
# to a live interface on this Mac before trying to publish port 8000.
# Without this, docker compose silently accepts an IP that's not live, then
# vpnkit fails to publish and the container appears healthy but is unreachable.

set -euo pipefail

IP="${CLOUDE_BIND_IP:-}"

if [[ -z "$IP" ]]; then
  echo "⚠ CLOUDE_BIND_IP is not set. Falling back to 127.0.0.1 (localhost only)."
  echo "  To expose on your LAN, set CLOUDE_BIND_IP in .env to one of:"
  ifconfig | awk '/^[a-z]/ { iface=$1 } /inet / && $2 != "127.0.0.1" && $2 !~ /^169\.254\./ { print "    " iface " -> " $2 }' | sed 's/://'
  exit 0
fi

if [[ "$IP" == "127.0.0.1" ]] || [[ "$IP" == "0.0.0.0" ]]; then
  echo "✓ CLOUDE_BIND_IP=$IP (loopback/wildcard)."
  exit 0
fi

# Grep-and-check: is the IP present in ifconfig output?
if ! ifconfig | grep -qw "$IP"; then
  echo "✗ CLOUDE_BIND_IP=$IP is NOT assigned to any live interface on this Mac."
  echo "  Did you switch networks? Current IPs:"
  ifconfig | awk '/^[a-z]/ { iface=$1 } /inet / && $2 != "127.0.0.1" && $2 !~ /^169\.254\./ { print "    " iface " -> " $2 }' | sed 's/://'
  echo ""
  echo "  Edit .env and set CLOUDE_BIND_IP to one of the above, then re-run."
  exit 1
fi

echo "✓ CLOUDE_BIND_IP=$IP is live."
exit 0
