#!/usr/bin/env bash
# Refresh the local Claude conversation archive, then report honestly.
#
# Purpose: one command to run when you are done working on a machine, so the
#   byte-exact archive catches up with whatever sessions you just created.
#   The app already ingests on startup and on a timer; this is the "I am
#   finished, catch up now and tell me the truth" button.
#
# Inputs:  none. Optional --verify N to byte-verify a random N archives.
# Output:  exit 0 archive current, 1 attention needed, 2 could not determine.
#          Three outcomes, never two: "I could not check" is not "fine".
# Example: ./scripts/refresh-archive.sh --verify 200
set -uo pipefail

PORT="${CLOUDECODE_PORT:-8000}"
HOST="${CLOUDECODE_HOST:-127.0.0.1}"
VERIFY="${2:-0}"
[ "${1:-}" = "--verify" ] && VERIFY="${2:-100}" || VERIFY=0

BASE="http://${HOST}:${PORT}/api/v1/corpus"

say() { printf '  %s\n' "$*"; }

# The server owns the datastore. Talking to it avoids two writers on one
# sqlite file, which is why this is an API call and not a direct ingest.
code=$(curl -s -o /tmp/refresh-arch.json -w '%{http_code}' --max-time 10 "${BASE}/status" 2>/dev/null)
if [ "$code" = "000" ]; then
    say "CANNOT DETERMINE: the app is not answering on ${HOST}:${PORT}."
    say "The archive may be perfectly fine - this says nothing about it."
    exit 2
fi
if [ "$code" = "401" ] || [ "$code" = "403" ]; then
    say "CANNOT DETERMINE: authentication required (HTTP ${code})."
    say "Set CLOUDECODE_TOKEN or run this from an authenticated context."
    exit 2
fi

say "triggering an ingest pass ..."
body='{}'
[ "$VERIFY" -gt 0 ] && body="{\"byte_verify_sample\": ${VERIFY}}"
curl -s -X POST --max-time 900 -H 'content-type: application/json' \
     -d "$body" "${BASE}/ingest" -o /tmp/refresh-arch-run.json \
     -w '' 2>/dev/null

python3 - <<'PY'
import json, sys
try:
    run = json.load(open('/tmp/refresh-arch-run.json'))
except Exception as exc:
    print(f"  CANNOT DETERMINE: could not read the run result ({exc})")
    sys.exit(2)
last = run.get('last_run', run)
def g(k, d='?'): return last.get(k, d)
print(f"  status            {g('status')}")
print(f"  files seen        {g('files_seen')}")
print(f"  newly ingested    {g('ingested')}")
print(f"  unchanged         {g('skipped_unchanged')}")
print(f"  could not read    {g('could_not_read')}")
bv = last.get('byte_verify') or {}
if bv:
    print(f"  byte-verified     {bv.get('hash_verified','?')} ok, "
          f"{bv.get('mismatch','?')} MISMATCH, {bv.get('could_not_evaluate','?')} unevaluable")
    if bv.get('mismatch'):
        print("  MISMATCH is the loudest thing this system can say. Investigate before trusting the archive.")
        sys.exit(1)
st = str(g('status'))
if st in ('ok', 'completed'):
    sys.exit(0)
print(f"  attention: run status was {st!r}")
sys.exit(1)
PY
