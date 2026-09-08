"""Detecting credential material in a transcript record, without ever
handling the credential.

WHY THIS EXISTS AND WHY IT DOES NOT REDACT. The owner has a live
1Password service-account token sitting in 308 message rows across 117
sessions, and has decided - on the record - not to rotate it until this
project ends. Redacting on the way in was rejected outright: the whole
point of the message model is that a stored record reproduces its
original bytes exactly, and a redacted record does not. So a record
carrying a credential is stored byte-exactly, and FLAGGED. The flag is
what makes the set enumerable, and an enumerable set is what turns the
eventual rotation from a hunt into a clean cut.

THE VALUE IS NEVER RETURNED, NEVER LOGGED, NEVER STORED. A finding
carries the detector's name, where in the text the match sat, and a
sha256 of the matched value - nothing else. There is no code path in this
module that puts a matched substring into a return value, an exception
message, a log line or a repr. The hash is there for one reason: it makes
"these 308 rows all carry the SAME credential" answerable without the
database becoming a second place that credential lives.

A REFERENCE IS NOT A SECRET, AND THAT DISTINCTION HAS ALREADY COST REAL
TIME. ``op://Claude/Paperless/api_token`` is a 1Password REFERENCE - a
pointer, safe to store, safe to commit, and exactly 31 characters, which
is short enough to look like a dead API token. Three separate agents in
this fleet mistook one for a credential on 2026-08-24 and one nearly
rotated a key that was never wrong. :data:`REFERENCE_PREFIXES` keeps that
class out of the findings, so the flagged set stays small enough to be
worth reading.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, List, Optional, Pattern, Tuple

#: Name of the detector that finds a 1Password service-account token. The
#: real token is ``ops_`` followed by a long base64 payload holding the
#: SRP secret, the MUK and the account key - it carries no timestamp of
#: any kind, so nothing about it can be dated from the value itself.
DETECTOR_OP_SERVICE_ACCOUNT: str = "op_service_account_token"

#: Name of the detector that finds a high-entropy value assigned to a
#: name that says it is a credential.
DETECTOR_HIGH_ENTROPY_ASSIGNMENT: str = "high_entropy_assignment"

#: Vendor-prefix detectors. Each of these keys off a marker the vendor
#: itself put in the credential, so they are far more precise than the
#: generic assignment detector and can be trusted on ordinary source.
DETECTOR_GITHUB_TOKEN: str = "github_token"
DETECTOR_AWS_ACCESS_KEY_ID: str = "aws_access_key_id"
DETECTOR_AWS_SECRET_ACCESS_KEY: str = "aws_secret_access_key"
DETECTOR_GOOGLE_API_KEY: str = "google_api_key"
DETECTOR_SLACK_TOKEN: str = "slack_token"
DETECTOR_PEM_PRIVATE_KEY: str = "pem_private_key"
DETECTOR_CLOUDFLARE_API_TOKEN: str = "cloudflare_api_token"

#: Detectors whose pattern contains a vendor-issued marker, so a match is
#: strong evidence on its own. These are safe to run over an entire
#: source tree; :data:`DETECTOR_HIGH_ENTROPY_ASSIGNMENT` is not, which is
#: why the two groups are named separately rather than lumped together.
VENDOR_DETECTORS: Tuple[str, ...] = (
    DETECTOR_OP_SERVICE_ACCOUNT,
    DETECTOR_GITHUB_TOKEN,
    DETECTOR_AWS_ACCESS_KEY_ID,
    DETECTOR_AWS_SECRET_ACCESS_KEY,
    DETECTOR_GOOGLE_API_KEY,
    DETECTOR_SLACK_TOKEN,
    DETECTOR_PEM_PRIVATE_KEY,
    DETECTOR_CLOUDFLARE_API_TOKEN,
)

ALL_DETECTORS: Tuple[str, ...] = VENDOR_DETECTORS + (
    DETECTOR_HIGH_ENTROPY_ASSIGNMENT,
)

#: Prefixes that mark a value as a POINTER at a secret rather than the
#: secret. Matching one of these excludes the value from every detector.
REFERENCE_PREFIXES: Tuple[str, ...] = ("op://", "vault://", "keychain://")

#: Substrings that mark a value as a stand-in rather than a credential.
#: Lowercased before comparison. These are the shapes that fill README
#: snippets, ``.env.example`` files and test fixtures, and flagging them
#: is how a scanner earns a reputation for crying wolf.
PLACEHOLDER_MARKERS: Tuple[str, ...] = (
    "example", "placeholder", "your", "yours", "changeme", "change_me",
    "dummy", "sample", "redacted", "notreal", "fake", "test", "xxxx",
    "0000", "1234", "abcd", "deadbeef", "insert", "replace", "todo",
    "fixme", "somekey", "sometoken", "secretgoeshere", "goeshere",
)

#: Shapes that mean "the value lives somewhere else". A commit carrying
#: ``token = os.environ["GH_TOKEN"]`` is the CORRECT handling of a
#: credential, and a scanner that blocks it teaches people to bypass it.
_INDIRECTION_RE = re.compile(
    r"""(?ix)
    ^(?:
        \$\{?[A-Za-z_][A-Za-z0-9_]*\}?     # $VAR or ${VAR}
      | %[A-Za-z_][A-Za-z0-9_]*%           # %VAR% (windows)
      | os\.environ.*                      # python
      | process\.env.*                     # node
      | \{\{.*\}\}                         # template placeholder
      | <[^>]*>                            # <your-token-here>
      | \.\.\.+
    )$
    """
)

#: Minimum entropy for a VENDOR-prefixed payload. Lower than the generic
#: threshold on purpose: the vendor marker has already done most of the
#: discrimination, so this only has to reject a payload of padding.
MIN_VENDOR_ENTROPY_BITS_PER_CHAR: float = 3.0

#: Longest run of one repeated character tolerated inside a credential
#: payload. ``ghp_aaaaaaaaaaaa...`` is documentation, not a token.
MAX_REPEATED_RUN: int = 4

#: An ``ops_`` token, requiring enough payload that a mention of the
#: literal string ``ops_`` in prose cannot match. The lookbehind is
#: load-bearing and was added after a measurement, not in theory: without
#: it the pattern fired on the python function name
#: ``test_router_drops_emit_when_all_three_channels_unconfigured``, where
#: ``drops_`` supplies the prefix and the rest of the identifier supplies
#: 41 characters of payload. ``ops_`` has to START a word.
_OP_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])ops_[A-Za-z0-9+/=_-]{40,}")

#: NAME = VALUE where the name says "credential". The name must END in
#: one of the credential words (or be one), so ``KEYBOARD_LAYOUT`` and
#: ``PASSWORD_PROMPT_TEXT`` do not match while ``API_KEY``,
#: ``anthropic_api_key`` and ``"secret"`` do. The value is taken up to
#: the first quote, whitespace, comma or brace, so a JSON string value
#: and a shell assignment are both covered by one pattern.
_ASSIGNMENT_RE = re.compile(
    r"""(?ix)
    [\"']?
    (?P<name>[A-Za-z0-9_.\-]*
             (?:token|secret|key|password|passwd|api_key|apikey))
    [\"']?
    \s*[:=]\s*
    [\"']?
    (?P<value>[A-Za-z0-9+/=_\-.]{20,})
    """
)

#: Minimum Shannon entropy, in bits per character, for an assigned value
#: to count as credential material. Measured against the two populations
#: that matter: a base64/hex credential sits comfortably above 4.0, while
#: the false positives this threshold exists to reject - a repeated
#: placeholder, a dotted module path, a long snake_case identifier - sit
#: below it. 3.5 is deliberately conservative: this flag exists to make a
#: set enumerable for a human, so an extra row in the list costs a glance
#: and a missed row costs a credential.
MIN_ENTROPY_BITS_PER_CHAR: float = 3.5


@dataclass(frozen=True)
class _VendorPattern:
    """One vendor-marker detector: a name, a regex, and which group holds
    the value.

    - ``detector``: the name reported, one of :data:`VENDOR_DETECTORS`.
    - ``regex``: compiled pattern. It must define a group named ``value``
      OR set ``whole_match`` so group 0 is taken.
    - ``check_payload``: whether the matched value goes through the
      placeholder and entropy guards. A PEM header has neither entropy
      nor a payload, so it opts out.
    """

    detector: str
    regex: Pattern[str]
    check_payload: bool = True
    require_mixed_alphabet: bool = False


#: A GitHub token. The five prefixes are personal access (``ghp_``),
#: OAuth (``gho_``), user-to-server (``ghu_``), server-to-server
#: (``ghs_``) and refresh (``ghr_``). The payload is base62 and at least
#: 36 characters in every format GitHub has shipped.
_GITHUB_RE = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")

#: An AWS access key id. The four-character prefix encodes the resource
#: type; AKIA (long-lived) and ASIA (temporary) are the two that appear
#: in practice, the rest are included so a paste of any of them is caught.
_AWS_KEY_ID_RE = re.compile(
    r"\b(?:AKIA|ASIA|ABIA|ACCA|AGPA|AIDA|AIPA|ANPA|ANVA|AROA)[A-Z0-9]{16}\b"
)

#: An AWS SECRET access key has no vendor marker at all - it is 40
#: characters of base64 and nothing else, which is why a bare pattern for
#: it is unusable. This one is CONTEXTUAL: the name beside it has to say
#: AWS. That trades some recall for a false-positive rate low enough that
#: the hook survives contact with a real repository.
_AWS_SECRET_RE = re.compile(
    r"""(?ix)
    aws[A-Za-z0-9_.\-]*(?:secret|access)[A-Za-z0-9_.\-]*
    [\"']? \s* [:=] \s* [\"']?
    (?P<value>[A-Za-z0-9/+=]{40})
    (?![A-Za-z0-9/+=])
    """
)

#: A Google API key: the literal ``AIza`` followed by 35 url-safe base64
#: characters. Google's own docs use this shape, so the placeholder guard
#: matters here more than for most.
_GOOGLE_API_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")

#: A Slack token. ``xoxb`` bot, ``xoxa`` app, ``xoxp`` user, ``xoxr``
#: refresh, ``xoxs`` (legacy) workspace.
_SLACK_RE = re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b")

#: A PEM private key block. Only the HEADER is MATCHED, deliberately: the
#: finding then carries a hash of the header rather than of key material,
#: so even the hash column cannot become a place a private key partially
#: lives. Base64 body is required by LOOKAHEAD rather than by match, so
#: it constrains without being captured. That requirement is not
#: pedantry - a bare header with nothing after it is what test fixtures
#: and documentation contain, and it was the second of the two false
#: positives measured across this repository.
_PEM_RE = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"
    r"(?=[\r\n\s]+[A-Za-z0-9+/=]{20,})"
)

#: A Cloudflare credential. Cloudflare API tokens are 40 characters of
#: ``[A-Za-z0-9_-]`` with NO vendor marker, and the legacy Global API Key
#: is 37 hex characters - both shapes collide with git sha fragments,
#: minified identifiers and base64 chunks, so a bare pattern for either
#: is unusable on a source tree. This is contextual for the same reason
#: the AWS secret detector is: the name beside it has to say Cloudflare.
_CLOUDFLARE_RE = re.compile(
    r"""(?ix)
    (?: cloudflare | \bcf ) [A-Za-z0-9_.\-]*
    (?: token | key | secret )
    [\"']? \s* [:=] \s* [\"']?
    (?P<value>[A-Za-z0-9_\-]{37,40})
    (?![A-Za-z0-9_\-])
    """
)

#: The vendor detector table. One row per detector, so adding a vendor is
#: a row rather than a new branch in :func:`scan_text`.
_VENDOR_PATTERNS: Tuple[_VendorPattern, ...] = (
    _VendorPattern(
        DETECTOR_OP_SERVICE_ACCOUNT, _OP_TOKEN_RE, require_mixed_alphabet=True,
    ),
    _VendorPattern(
        DETECTOR_GITHUB_TOKEN, _GITHUB_RE, require_mixed_alphabet=True,
    ),
    _VendorPattern(DETECTOR_AWS_ACCESS_KEY_ID, _AWS_KEY_ID_RE),
    _VendorPattern(DETECTOR_AWS_SECRET_ACCESS_KEY, _AWS_SECRET_RE),
    _VendorPattern(
        DETECTOR_GOOGLE_API_KEY, _GOOGLE_API_KEY_RE,
        require_mixed_alphabet=True,
    ),
    _VendorPattern(DETECTOR_SLACK_TOKEN, _SLACK_RE),
    _VendorPattern(DETECTOR_PEM_PRIVATE_KEY, _PEM_RE, check_payload=False),
    _VendorPattern(DETECTOR_CLOUDFLARE_API_TOKEN, _CLOUDFLARE_RE),
)


def shannon_entropy(text: str) -> float:
    """Shannon entropy of a string, in bits per character.

    Description: the standard measure over the string's own character
      distribution. Returns 0.0 for the empty string rather than raising,
      because an empty candidate is simply not credential material.
    Inputs: text (str).
    Output: float - bits per character, 0.0 for an empty string.
    Example: round(shannon_entropy("aaaa"), 3) -> 0.0
    """
    if not text:
        return 0.0
    counts = Counter(text)
    total = len(text)
    return -sum(
        (n / total) * math.log2(n / total) for n in counts.values()
    )


def _is_reference(value: str) -> bool:
    """Whether a value is a pointer at a secret rather than the secret.

    Description: see REFERENCE_PREFIXES and this module's docstring for
      why this distinction is load-bearing rather than a nicety.
    Inputs: value (str).
    Output: bool.
    Example: _is_reference("op://Claude/Gogs/api_token") -> True
    """
    return any(value.startswith(p) for p in REFERENCE_PREFIXES)


def _is_indirection(value: str) -> bool:
    """Whether a value names where the credential lives instead of being it.

    Description: ``${GH_TOKEN}``, ``os.environ["X"]``, ``<your-token>``
      and friends. This is the shape of code that handles a credential
      CORRECTLY, so blocking it would punish the behaviour the scanner
      exists to encourage.
    Inputs: value (str).
    Output: bool.
    Example: _is_indirection("${CF_API_TOKEN}") -> True
    """
    return bool(_INDIRECTION_RE.match(value.strip()))


def _has_long_repeat(value: str) -> bool:
    """Whether the value contains a run of one character long enough to
    mean it is padding.

    Description: a documentation stand-in is usually a row of the same
      character. A real base64 credential effectively never carries a run
      past MAX_REPEATED_RUN.
    Inputs: value (str).
    Output: bool.
    Example: _has_long_repeat("ghp_" + "x" * 40) -> True
    """
    run = 1
    for prev, char in zip(value, value[1:]):
        run = run + 1 if char == prev else 1
        if run > MAX_REPEATED_RUN:
            return True
    return False


def has_mixed_alphabet(value: str) -> bool:
    """Whether a value carries both an uppercase letter and a digit.

    Description: the cheap discriminator between a base62 credential
      payload and a snake_case identifier that happens to be long. A
      random 36-character base62 string omits every uppercase letter with
      probability around 1e-8, so requiring one costs no recall worth
      measuring, while an identifier fails it every time.
    Inputs: value (str).
    Output: bool.
    Example: has_mixed_alphabet("emit_when_all_three_channels") -> False
    """
    return any(c.isupper() for c in value) and any(c.isdigit() for c in value)


def is_placeholder(value: str) -> bool:
    """Whether a credential-shaped value is a stand-in rather than a credential.

    Description: the single gate every detector runs its match through.
      Three independent reasons to reject - a 1Password style reference,
      an environment-variable indirection, and documentation padding
      (a placeholder word, or a long repeated run). It is deliberately
      one function so a new detector cannot be added without inheriting
      all three; a divergent second copy of this logic is how a scanner
      starts firing on README files.
    Inputs: value (str) - the matched text.
    Output: bool - True when the value must NOT be reported.
    Example: is_placeholder("AIzaSyYOUR_API_KEY_HERE_xxxxxxxxxxxxxxx") -> True
    """
    if _is_reference(value) or _is_indirection(value):
        return True
    if _has_long_repeat(value):
        return True
    lowered = value.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


@dataclass(frozen=True)
class SecretFinding:
    """One credential match, described without the credential.

    - ``detector``: which detector fired, one of ALL_DETECTORS.
    - ``offset`` / ``length``: where the match sat in the searched text.
    - ``value_sha256``: sha256 of the matched value, so two records
      carrying the same credential are recognisable as one credential.

    There is deliberately no field for the value. Adding one would make
    every caller, log line and test fixture a place the credential can
    escape to.
    """

    detector: str
    offset: int
    length: int
    value_sha256: str

    def __post_init__(self) -> None:
        if self.detector not in ALL_DETECTORS:
            raise ValueError(f"unknown detector: {self.detector!r}")
        if self.length <= 0:
            raise ValueError("SecretFinding.length must be positive")


def _finding(detector: str, value: str, offset: int) -> SecretFinding:
    """Build a SecretFinding from a matched value without retaining it.

    Description: the single construction site, so hashing can never be
      forgotten at one call site and remembered at another.
    Inputs: detector (str), value (str - the matched text, used only to
      hash and measure), offset (int).
    Output: SecretFinding.
    Example: _finding(DETECTOR_OP_SERVICE_ACCOUNT, "ops_" + "a" * 40, 0)
      .length -> 44
    """
    return SecretFinding(
        detector=detector,
        offset=offset,
        length=len(value),
        value_sha256=hashlib.sha256(value.encode("utf-8")).hexdigest(),
    )


def scan_text(
    text: str, detectors: Optional[Iterable[str]] = None,
) -> List[SecretFinding]:
    """Find credential material in one block of text.

    Description: runs the vendor-marker detectors first, then the generic
      high-entropy assignment detector over whatever span the vendor
      detectors did not already claim. A credential therefore produces
      exactly ONE finding even when several patterns describe it - a
      count that double-reports is a count nobody can act on.

      ``detectors`` lets a caller narrow the set. That exists because the
      two groups have genuinely different precision: the vendor detectors
      key off a marker the vendor itself issued and are safe over an
      entire source tree, while the generic assignment detector is tuned
      for transcript bodies and fires on ordinary source. Narrowing is
      the supported way to trade recall for precision; writing a second,
      divergent copy of these patterns is not.
    Inputs: text (str) - the text to search, typically one record's
      rendered body JSON or one file. detectors (iterable of str or
      None) - which detector names to run, defaulting to ALL_DETECTORS.
    Output: list[SecretFinding], ordered by offset. Empty when nothing
      matched, which is a real negative result and not a "could not
      determine" - the search ran over the whole string.
    Example: len(scan_text("token=" + "op://Claude/x/y")) -> 0
    """
    wanted = frozenset(ALL_DETECTORS if detectors is None else detectors)
    unknown = wanted - frozenset(ALL_DETECTORS)
    if unknown:
        raise ValueError(f"unknown detector(s): {sorted(unknown)!r}")

    findings: List[SecretFinding] = []
    claimed: List[Tuple[int, int]] = []

    for pattern in _VENDOR_PATTERNS:
        if pattern.detector not in wanted:
            continue
        for match in pattern.regex.finditer(text):
            has_value = "value" in (match.groupdict() or {})
            group = "value" if has_value else 0
            value = match.group(group)
            if pattern.check_payload and (
                is_placeholder(value)
                or shannon_entropy(value) < MIN_VENDOR_ENTROPY_BITS_PER_CHAR
            ):
                continue
            if pattern.require_mixed_alphabet and not has_mixed_alphabet(value):
                continue
            findings.append(
                _finding(pattern.detector, value, match.start(group))
            )
            claimed.append((match.start(), match.end()))

    if DETECTOR_HIGH_ENTROPY_ASSIGNMENT in wanted:
        for match in _ASSIGNMENT_RE.finditer(text):
            value = match.group("value")
            start = match.start("value")
            end = match.end("value")
            if any(
                start < c_end and c_start < end for c_start, c_end in claimed
            ):
                continue
            if is_placeholder(value):
                continue
            if shannon_entropy(value) < MIN_ENTROPY_BITS_PER_CHAR:
                continue
            findings.append(
                _finding(DETECTOR_HIGH_ENTROPY_ASSIGNMENT, value, start)
            )

    findings.sort(key=lambda f: f.offset)
    return findings
