#!/bin/bash
# Mutation check for the bind-address list (macOS/network-interfaces.js) and
# the secure/insecure indicator (macOS/tls-status.js).
#
# Two defect classes, both silent. The first is an address that is simply
# ABSENT from a menu, which looks exactly like not having one: that is how the
# Tailscale address went missing behind a /^utun/ name blocklist. The second
# is a padlock rendered from the URL scheme rather than from a measured
# certificate, including the case that matters most, a perfectly in-date
# certificate for the WRONG hostname.
#
# Oracles: tests/test_network_interfaces.node.mjs and
# tests/test_tls_status.node.mjs, both of which must be green before anything
# is mutated.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/scripts/ci/lib/mutate-trap.sh"
cd "$ROOT" || exit 1

OUT=/tmp/mutate-bind-tls-out.log
FILES=("macOS/network-interfaces.js" "macOS/tls-status.js")
mutate_arm_trap "$ROOT" "${FILES[@]}"

survived=0; cannot_determine=0; killed=0

run_check() {
  mutate_run node "$ROOT/tests/test_network_interfaces.node.mjs" >"$OUT" 2>&1
  local a=$?
  mutate_run node "$ROOT/tests/test_tls_status.node.mjs" >>"$OUT" 2>&1
  local b=$?
  if [ "$a" -ne 0 ] || [ "$b" -ne 0 ]; then return 1; fi
  return 0
}

echo "--- baseline ---"
run_check
if [ $? -ne 0 ]; then echo "BASELINE IS RED. Refusing to run."; tail -20 "$OUT"; exit 2; fi
echo "baseline green"

mutate() {
  local name="$1" file="$2" expr="$3"
  mutate_restore_files
  python3 - "${ROOT}/${file}" "$expr" <<'PYEOF'
import sys
path, expr = sys.argv[1], sys.argv[2]
text = open(path, encoding='utf-8').read()
old, new = expr.split('||=>||')
if old not in text:
    sys.exit('mutation target not found: ' + old[:70])
open(path, 'w', encoding='utf-8').write(text.replace(old, new, 1))
PYEOF
  if [ $? -ne 0 ]; then
    echo "CANNOT_DETERMINE $name (anchor stale)"; cannot_determine=$((cannot_determine+1)); return
  fi
  run_check
  local status=$?
  if [ "$status" -eq 1 ]; then killed=$((killed+1)); echo "killed   $name"; return; fi
  if [ "$status" -ne 0 ]; then
    echo "CANNOT_DETERMINE $name (exit $status)"; cannot_determine=$((cannot_determine+1)); return
  fi
  echo "SURVIVED $name"; survived=$((survived+1))
}

echo "--- the bind list: reintroducing the name blocklist ---"

# NOTE ON AN EXCLUDED MUTATION. A version of the utun-blocklist mutant was
# written against isBindableAddress() as
#   if (addr.__iface && /^utun/i.test(addr.__iface)) return false;
# and it SURVIVED. That is correct and the suite was not weakened to force it
# to die: isBindableAddress() receives a single address entry and never the
# interface name, so `addr.__iface` is always undefined, the condition can
# never fire, and the mutant changes no behaviour at all. It is an equivalent
# mutant. The blocklist defect can only be expressed where the interface name
# is actually in scope, which is the listing loop below, and that mutant is
# killed.

mutate "the utun name check is applied in the listing loop" \
  "macOS/network-interfaces.js" \
  "      if (!isBindableAddress(addr)) continue;||=>||      if (/^utun/i.test(name)) continue;
      if (!isBindableAddress(addr)) continue;"

mutate "loopback is offered as a bind target" \
  "macOS/network-interfaces.js" \
  "  if (addr.internal) return false;||=>||  if (false) return false;"

mutate "link-local self-assigned addresses are offered" \
  "macOS/network-interfaces.js" \
  "  if (addr.address.startsWith(LINK_LOCAL_V4_PREFIX)) return false;||=>||  if (false) return false;"

mutate "IPv6 addresses are offered as bind targets" \
  "macOS/network-interfaces.js" \
  "  if (!isV4) return false;||=>||  if (false) return false;"

mutate "the numeric family form newer Node reports is rejected" \
  "macOS/network-interfaces.js" \
  "  const isV4 = addr.family === 'IPv4' || addr.family === 4;||=>||  const isV4 = addr.family === 'IPv4';"

mutate "the Tailscale CGNAT range is mislabelled" \
  "macOS/network-interfaces.js" \
  "  return first === 100 && second >= 64 && second <= 127;||=>||  return first === 100;"

echo "--- the padlock: claiming security nobody measured ---"

mutate "an https URL alone earns a padlock with no certificate" \
  "macOS/tls-status.js" \
  "  const cert = input && input.cert;
  if (!cert) {||=>||  const cert = input && input.cert;
  if (false) {"

mutate "plain HTTP is reported as secure" \
  "macOS/tls-status.js" \
  "  if (scheme !== 'https') {||=>||  if (false) {"

mutate "the certificate NAME is never compared to the hostname" \
  "macOS/tls-status.js" \
  "  if (!certCoversHost(host, cert)) {||=>||  if (false) {"

mutate "a wrong-name certificate is softened to cannot-determine" \
  "macOS/tls-status.js" \
  "      level: LEVEL_INSECURE,
      label: 'Connection: not secure (wrong certificate)',||=>||      level: LEVEL_UNKNOWN,
      label: 'Connection: not secure (wrong certificate)',"

mutate "an expired certificate is accepted" \
  "macOS/tls-status.js" \
  "  if (notAfter <= now) {||=>||  if (false) {"

mutate "CN is consulted even when a SAN exists, unlike a browser" \
  "macOS/tls-status.js" \
  "  if (sans.length > 0) return sans.some((san) => nameMatches(host, san));||=>||  if (sans.length > 0 && sans.some((san) => nameMatches(host, san))) return true;"

mutate "a wildcard matches more than one label" \
  "macOS/tls-status.js" \
  "  return label.length > 0 && !label.includes('.');||=>||  return label.length > 0;"

mutate "a certificate probe error is reported as secure" \
  "macOS/tls-status.js" \
  "  if (input && input.certError) {||=>||  if (false) {"

mutate_restore_files
echo
echo "killed ${killed}, survived ${survived}, cannot_determine ${cannot_determine}"
if [ "$survived" -ne 0 ] || [ "$cannot_determine" -ne 0 ]; then
  echo "MUTATION CHECK FAILED"; exit 1
fi
echo "MUTATION CHECK PASSED"
