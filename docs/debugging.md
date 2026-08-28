# Debugging CloudeCode

## The one switch

**From source:**

```bash
CLOUDE_DEBUG=1
```

**For the packaged `.app`,** add it to your `.env` instead:

```
CLOUDE_DEBUG=1
```

The `.app` is a GUI application whose server subprocess is spawned with a
constructed environment, so an exported shell variable — or even
`launchctl setenv` — does not reach it. That was measured: the flag was
set and no trace file appeared. `.env` is how this app is configured, and
`Settings` already reads it.

Set it, restart the app, and the paths that fail silently start writing to
`<state_dir>/debug/trace.jsonl` — on macOS that is
`~/Library/Application Support/CloudeCode/debug/trace.jsonl`.

Turn it off by unsetting it. Delete the file whenever you like; nothing
reads it back.

## Why it is not just `LOG_LEVEL=DEBUG`

`LOG_LEVEL` is a blunt global. Turning it to DEBUG also unmutes every
per-second poller — `idle_watcher.poll_suppressed` alone fires roughly
once a second per open session, and under launchd that sink is
`launchd.log`, which the process holds open across rotation. That is how
the log became unreadable and unbounded, and it is why the filtering bound
logger was added in the first place.

`CLOUDE_DEBUG` is a separate axis. It is verbose about a handful of
interesting call sites and says nothing about anything else, and it writes
to its own file that nothing rotates out from under it.

## What it exists for

Not exceptions. Exceptions already log. This is for the failures that look
exactly like successes:

- a hook that fires, exits 0, and delivers nothing
- a spawn whose environment is present but **stale**
- a lookup that returns an empty list because it read the wrong attribute

None of those raise, so ordinary error logging never sees them.

**The incident that caused this file.** Claude's SessionStart hook
delivered nothing for every session on a real install. The hook was
registered, it ran, it exited 0, the endpoint was reachable, and the
environment was correct. Finding it meant bisecting a shell one-liner by
hand, because every layer reported success and none recorded what it
actually saw. The cause was a trailing `&` that orphaned curl before it
delivered — invisible from every log in the system.

## What is instrumented

| event | answers |
|---|---|
| `hook.lifecycle.received` | did the hook arrive, with what body, carrying which claude session id |
| `hook.lifecycle.outcome` | bound / continued / forked / **unresolved**, and why |
| `hook.lifecycle.threw` | it raised, with the type |
| `lineage.record.input` | the socket, name, epoch and uuid the resolver was given |
| `tmux.new_session` | the argv, the env keys passed, the `-e` pairs, the session id |
| `tmux.new_session.result` | return code and stderr |

## Reading it

One JSON object per line.

```bash
# did the hook ever arrive?
grep hook.lifecycle "$HOME/Library/Application Support/CloudeCode/debug/trace.jsonl"

# every spawn and the session id it was given
python3 -c '
import json,sys
for line in open(sys.argv[1]):
    r = json.loads(line)
    if r["event"].startswith("tmux."):
        print(r["ts"], r["event"], r.get("cloudecode_session_id"), r.get("dash_e_pairs"))
' "$HOME/Library/Application Support/CloudeCode/debug/trace.jsonl"
```

## Secrets

Any field whose **key** contains `token`, `secret`, `key`, `password`,
`authorization` or `cookie` is replaced with a length and a short
fingerprint — never the value. Redaction is by key name rather than by
inspecting the value, so a credential is caught even when it looks
innocuous.

That is deliberate: this file exists to be pasted into a conversation
while debugging, so a token in it is a token in a transcript. The
fingerprint still answers the only question worth asking — *is this the
same value as that one?* — and the length is there because a wrong-shaped
secret is a common bug that length alone often names.

## Bounds

Single values are truncated past 600 characters. The file rotates once to
`trace.jsonl.1` at 8MB, so an overnight session cannot fill a disk and the
file you open is always the newest data.

Every entry point swallows its own errors. A tracer that can break the
thing it is tracing is worse than no tracer, and this one runs inside
request handlers and a subprocess spawn path.

## The launch picker asks TWO questions about models

`accepts_model` (per wrapper) and `needs_model` (per family) are not the
same question and neither implies the other:

- `accepts_model` - does this wrapper take a model id as `$1`?
  True for `cldor` AND `cldl`.
- `needs_model` - must the id come from the LOCAL box, and is a bare
  launch refused? True only for the `local` family.

The first says a model step exists. The second says WHICH catalog it
opens. Testing only the first sends every LM Studio launch to the
OpenRouter list - that was the v1.0.26 fix.

A wrapper inherits `needs_model` from its family, and the wrapper branch
tests it FIRST, because both flags are true for `cldl` and order is the
whole decision.

Worth knowing about the shape: a family only contributes a pinned row when
it has NO wrappers. So any check written on the pinned-family path is
silently unreachable for a family the user has authored a wrapper for -
which is every family in a real install eventually.
