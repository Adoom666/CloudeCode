# Secret scanning

A pre-commit hook that refuses a commit staging credential material, and
an audit mode for checking the tree as it stands.

## Why this exists

On 2026-08-30 a Cloudflare API token was flagged in a public GitHub
repository. It came from commits made by the project's original author in
2025, was carried along by a fork, and surfaced only when a pull request
replicated the history into an account where a scanner was watching. The
repository was public the whole time, so the exposure was old and only the
detection was new. Separately, a full-corpus scan the same day found 731
distinct credentials across 6,080 transcript message bodies.

Two exposure classes in one day, and nothing looked at a commit before it
was pushed. This is that look.

## Install and uninstall

```sh
./scripts/install-secret-hook.sh      # writes .git/hooks/pre-commit
./scripts/uninstall-secret-hook.sh    # removes it
```

`.git/hooks` is not version controlled, so a hook committed to the
repository does nothing until it is copied into place. The installer is
the distribution mechanism, and it has to be run once per clone.

The installer prints the exact path it writes. It refuses to overwrite a
`pre-commit` hook it did not write; `--force` backs that hook up first,
and the uninstaller restores the backup. The uninstaller likewise refuses
to delete a hook it does not recognise.

To bypass the hook for one commit:

```sh
git commit --no-verify
```

That is documented deliberately. A hook nobody can get past in an
emergency is a hook that gets deleted instead of bypassed, and then there
is no scanner at all. If you bypass it because it was wrong, say so, so
the detector can be fixed rather than routed around.

## What the hook scans

Only the lines the commit **adds**, read from `git diff --cached -U0`.

A credential already sitting in a file you happened to touch does not
block you. That is a deliberate limit, not an oversight: blocking on
somebody else's old line is the fastest way to get a hook uninstalled,
and the audit mode below is the tool for pre-existing content.

Timing on this repository, measured: a three-file commit scans in 0.084
seconds, and a full-tree audit of 907 files takes 1.02 seconds.

## Audit mode

```sh
./venv/bin/python3 scripts/scan_secrets.py                  # whole repo
./venv/bin/python3 scripts/scan_secrets.py src client       # given paths
./venv/bin/python3 scripts/scan_secrets.py --json           # machine output
./venv/bin/python3 scripts/scan_secrets.py --no-excerpt     # position only
./venv/bin/python3 scripts/scan_secrets.py --all-detectors  # noisy, see below
./venv/bin/python3 scripts/scan_secrets.py --no-pragma      # ignore opt-outs
```

History is **not** scanned, by default or otherwise. This repository has
over 600 commits and a history scan is a different job with a different
cost; the tooling here deliberately does not pretend to do it.

## Exit codes are three-valued

| code | meaning |
|---|---|
| 0 | scanned, found nothing |
| 1 | scanned, found credential material (commit refused) |
| 2 | **could not scan** (commit refused, and it says so) |

2 is not 0. A scanner that returns success because it never ran is the
exact false green this project's monitoring has spent months removing.
The hook exits 2 when git fails, when the scanner file is missing, or
when there is no `python3` - and refuses the commit in every case.

## The detectors

`src/core/message_model_secrets.py` is the single source of truth for
what counts as a secret. It is shared with the transcript message model,
so a pattern added for the hook improves that too. Do not write a second
set of patterns anywhere.

Vendor-marker detectors, run by default on files:

| detector | what it keys off |
|---|---|
| `op_service_account_token` | `ops_` plus a long base64 payload |
| `github_token` | `ghp_` `gho_` `ghu_` `ghs_` `ghr_` plus 36+ base62 |
| `aws_access_key_id` | `AKIA` / `ASIA` and the other AWS prefixes plus 16 |
| `aws_secret_access_key` | 40 base64 chars beside a name saying AWS |
| `google_api_key` | `AIza` plus 35 url-safe base64 |
| `slack_token` | `xoxb` `xoxa` `xoxp` `xoxr` `xoxs` plus a payload |
| `pem_private_key` | a PRIVATE KEY header followed by real base64 body |
| `cloudflare_api_token` | 37 to 40 chars beside a name saying Cloudflare |

Two of these are **contextual** rather than prefix-matched, because the
credential itself carries no marker. An AWS secret access key is 40
characters of base64 and nothing else; a Cloudflare API token is 40
characters of `[A-Za-z0-9_-]`. Both shapes collide with git sha
fragments, minified identifiers and ordinary base64, so a bare pattern
for either is unusable on a source tree. They require a name beside them
that says AWS or Cloudflare. That trades recall for a false-positive rate
low enough to survive contact with a real repository, and the trade is
the honest one: a scanner nobody keeps installed detects nothing at all.

The PEM detector matches only the **header** and requires base64 body by
lookahead. The finding therefore carries a hash of the header, never of
key material, so the finding record cannot become a second place a
private key partially lives.

### The generic detector, and why it is off by default

`high_entropy_assignment` fires on any high-entropy value assigned to a
name ending in a credential word. It is excellent over transcript bodies
and unusable over source. Measured across this working tree:

- default vendor detectors: **0 findings** over 907 files, 1.02s
- `--all-detectors`: **47 findings** over the same files, 4.77s

Every one of the 47 was a false positive. They were things like
`const STORAGE_KEY = 'cloude.sidebar.arrangement'`, `token = secrets.token_hex(32)`
and `guard let token = keychain() else {`. A 100 percent false-positive
rate is a scanner nobody reads, so it is available behind an explicit
flag and it is not what the hook runs.

## The inline pragma, and why it is not a path allowlist

A line carrying `secret-scan: allow <reason>` is not reported. The reason
is required; a bare `secret-scan: allow` suppresses nothing.

Something in this repository has to contain credential-shaped strings,
because a detector's positive control cannot exist otherwise, and a
detector with no positive control is one nobody has shown can fire. The
obvious fix is to exclude the test files by path. That is the wrong fix:
it blinds the scanner to an entire file forever, including to whatever
gets added to that file next year. A pragma is per line, sits in the diff
where a reviewer sees it, and demands a written reason.

Suppressions are **counted and printed**, in both modes and in the JSON,
so a suppression can never become invisible:

```
clean: 907 files scanned, 111 skipped, 6 suppressed by inline pragma, 8 detectors, 1.02s
```

Those 6 are the synthetic fixtures in `tests/test_secret_detectors.py`
and `tests/test_secret_scan_files.py`, and nothing else. `--no-pragma`
lists them, which is how you audit the suppressions themselves.

The pragma lives in `src/core/secret_scan.py`, not in the detector
module. That is deliberate: a transcript body quoting the comment must
not be able to suppress its own flagging.

## What is never printed

No matched value, anywhere: not in output, not in an error message, not
in a log, not in the JSON. A finding carries path, line, column, detector
name, length and a sha256 prefix. The optional excerpt is built by
masking **every** matched span on the line before the line is truncated,
so a line carrying two credentials cannot print one while reporting the
other. `--no-excerpt` drops even that.

`tests/test_secret_detectors.py` and `tests/test_secret_scan_files.py`
assert this, and each absence assertion is paired with a positive control
asserting the detector name and file path **did** appear - otherwise the
test would pass against a scanner that printed nothing at all.

## Placeholders, references and indirection

The scanner must not fire on the correct way to handle a credential.
Three classes are excluded, in `is_placeholder()`:

- **1Password references**: `op://Claude/GitHub/token`, and the
  `vault://` and `keychain://` equivalents. This fleet nearly rotated a
  correct credential on 2026-08-24 because an `op://` reference, which is
  exactly 31 characters, was mistaken for a dead API token. Flagging a
  reference teaches people to stop using references.
- **Environment indirection**: `${CF_API_TOKEN}`, `%VAR%`,
  `os.environ["X"]`, `process.env.X`, `{{template}}`, `<your-token>`.
- **Documentation padding**: a placeholder word (`example`, `your`,
  `changeme`, `redacted`, and the rest of `PLACEHOLDER_MARKERS`), or a
  run of more than four identical characters.

## Tuning already applied

Two false positives were measured on this tree and fixed at the pattern,
not by an ignore list:

1. `ops_` matched inside the python function name
   `test_router_drops_emit_when_all_three_channels_unconfigured`, where
   `drops_` supplies the prefix and the rest supplies 41 characters of
   payload. Fixed with a lookbehind requiring `ops_` to start a word,
   plus a mixed-alphabet requirement (an uppercase letter and a digit)
   that a snake_case identifier can never satisfy and a base62 payload
   effectively always does.
2. A bare `-----BEGIN OPENSSH PRIVATE KEY-----` header in a test fixture,
   with no key after it. Fixed by requiring base64 body via lookahead.

An ignore list would have hidden both without saying anything true about
either. If the scanner fires on something correct, fix the pattern.

## Adding a detector

Add a row to `_VENDOR_PATTERNS` in `src/core/message_model_secrets.py`,
a name constant, and an entry in `VENDOR_DETECTORS`. Then add a row to
`CASES` in `tests/test_secret_detectors.py` carrying one sample that must
fire and the placeholder, reference and indirection samples that must
not. The parametrised tests pick it up automatically, and
`test_every_declared_detector_has_a_case_or_is_the_generic_one` fails if
you skip that step.
