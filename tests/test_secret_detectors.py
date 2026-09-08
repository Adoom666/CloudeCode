"""Tests for the vendor-marker credential detectors.

NO REAL CREDENTIAL APPEARS IN THIS FILE. Every fixture is synthetic:
built to have the SHAPE a vendor issues and none of the value. Any of
these strings can be pasted into a search engine safely.

EVERY NEGATIVE IS PAIRED WITH A POSITIVE. A detector that never fires and
a detector whose pattern is broken produce identical output, so a test
asserting "this must not match" is worthless on its own - it passes just
as happily against a regex that matches nothing at all. Each row of
:data:`CASES` therefore carries a sample that MUST fire alongside the
placeholder, reference and indirection samples that must not.

THE REFERENCE CASE IS THE ONE THAT MATTERS. This fleet nearly rotated a
correct credential on 2026-08-24 because an ``op://`` REFERENCE was read
as a secret. A scanner that flags the safe way of handling a credential
teaches people to stop handling credentials safely.
"""

from __future__ import annotations

import pytest

from src.core.message_model_secrets import (
    ALL_DETECTORS,
    DETECTOR_AWS_ACCESS_KEY_ID,
    DETECTOR_AWS_SECRET_ACCESS_KEY,
    DETECTOR_CLOUDFLARE_API_TOKEN,
    DETECTOR_GITHUB_TOKEN,
    DETECTOR_GOOGLE_API_KEY,
    DETECTOR_HIGH_ENTROPY_ASSIGNMENT,
    DETECTOR_PEM_PRIVATE_KEY,
    DETECTOR_SLACK_TOKEN,
    VENDOR_DETECTORS,
    has_mixed_alphabet,
    is_placeholder,
    scan_text,
)

# Synthetic payloads. Each has the vendor's marker, the vendor's length,
# and a body of arbitrary characters that is not any issued credential.
FAKE_GITHUB = "ghp_R7kQ2mVx9TbnLcJ6wZaHy8FdSpU3gEoNrKq1"  # secret-scan: allow synthetic fixture, not an issued credential
FAKE_AWS_ID = "AKIAZ3QN7XW2VLTR6BCF"  # secret-scan: allow synthetic fixture, not an issued credential
FAKE_AWS_SECRET = "hV8pQm2LzR4TnXw9CbKdY6JfAe3UgSoP1MiNrEuZ"  # secret-scan: allow synthetic fixture, not an issued credential
FAKE_GOOGLE = "AIzaSyD7kQ2mVx9TbnLcJ6wZaHy8FdSpU3gEoNr"  # secret-scan: allow synthetic fixture, not an issued credential
FAKE_SLACK = "xoxb-2847193056321-2847193056789-Kd7QmZx9TbnLcJ6wZaHy8Fd"  # secret-scan: allow synthetic fixture, not an issued credential
FAKE_CLOUDFLARE = "v1Qk7XmZr9TdNbLcJ6wZaHy8FdSpU3gEoNrKq"  # secret-scan: allow synthetic fixture, not an issued credential
FAKE_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEAy8Kq3nJd7VtWpZrXbLmQfHgN4TcUeSoA2iRvYxDkB9Fj\n"
    "-----END RSA PRIVATE KEY-----\n"
)

#: detector -> (fires, does-not-fire samples). The second element is the
#: three ways a credential is CORRECTLY represented in a commit: a
#: documentation placeholder, a 1Password reference, and an
#: environment-variable indirection.
CASES = {
    DETECTOR_GITHUB_TOKEN: (
        f"GITHUB_TOKEN={FAKE_GITHUB}",
        [
            "GITHUB_TOKEN=ghp_YOUR_PERSONAL_ACCESS_TOKEN_GOES_HERE_xx",
            "GITHUB_TOKEN=op://Claude/GitHub/personal_access_token",
            'GITHUB_TOKEN=${GH_TOKEN}',
            'github_token = os.environ["GH_TOKEN"]',
        ],
    ),
    DETECTOR_AWS_ACCESS_KEY_ID: (
        f"AWS_ACCESS_KEY_ID={FAKE_AWS_ID}",
        [
            "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
            "AWS_ACCESS_KEY_ID=op://Claude/AWS/access_key_id",
            "AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID}",
        ],
    ),
    DETECTOR_AWS_SECRET_ACCESS_KEY: (
        f"AWS_SECRET_ACCESS_KEY={FAKE_AWS_SECRET}",
        [
            "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "AWS_SECRET_ACCESS_KEY=op://Claude/AWS/secret_access_key",
            "AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY}",
        ],
    ),
    DETECTOR_GOOGLE_API_KEY: (
        f"GOOGLE_API_KEY={FAKE_GOOGLE}",
        [
            "GOOGLE_API_KEY=AIzaSyYOUR_GOOGLE_API_KEY_HERE_00000000",
            "GOOGLE_API_KEY=op://Claude/Google/api_key",
            "GOOGLE_API_KEY=${GOOGLE_API_KEY}",
        ],
    ),
    DETECTOR_SLACK_TOKEN: (
        f"SLACK_BOT_TOKEN={FAKE_SLACK}",
        [
            "SLACK_BOT_TOKEN=xoxb-0000000000-0000000000-YOUR-TOKEN-HERE",
            "SLACK_BOT_TOKEN=op://Claude/Slack/bot_token",
            "SLACK_BOT_TOKEN=${SLACK_BOT_TOKEN}",
        ],
    ),
    DETECTOR_CLOUDFLARE_API_TOKEN: (
        f"CLOUDFLARE_API_TOKEN={FAKE_CLOUDFLARE}",
        [
            "CLOUDFLARE_API_TOKEN=your-cloudflare-api-token-goes-here00",
            "CLOUDFLARE_API_TOKEN=op://Claude/Cloudflare/api_token",
            "CLOUDFLARE_API_TOKEN=${CLOUDFLARE_API_TOKEN}",
            'CF_API_TOKEN = process.env.CF_API_TOKEN',
        ],
    ),
    DETECTOR_PEM_PRIVATE_KEY: (
        FAKE_PEM,
        [
            "-----BEGIN OPENSSH PRIVATE KEY-----\n",
            "the file starts with -----BEGIN RSA PRIVATE KEY----- as expected",
            "private_key = op://Claude/Host/private_key",
        ],
    ),
}


@pytest.mark.parametrize("detector", sorted(CASES))
def test_the_detector_fires_on_a_synthetic_credential(detector):
    """The positive control for every negative test below it."""
    positive = CASES[detector][0]
    fired = {f.detector for f in scan_text(positive)}
    assert detector in fired, f"{detector} did not fire on its own sample"


@pytest.mark.parametrize(
    "detector,sample",
    [(d, s) for d, (_, negatives) in CASES.items() for s in negatives],
    ids=lambda v: str(v)[:48],
)
def test_a_placeholder_reference_or_indirection_is_not_a_secret(
    detector, sample,
):
    """Documentation stand-ins, op:// references and $ENV indirection are
    how a credential is SUPPOSED to appear in a commit."""
    fired = {f.detector for f in scan_text(sample)}
    assert detector not in fired, f"{detector} false-positived on {sample!r}"


@pytest.mark.parametrize("detector", sorted(CASES))
def test_one_credential_produces_exactly_one_finding(detector):
    """Several patterns can describe the same value. A count that
    double-reports is a count nobody can act on."""
    assert len(scan_text(CASES[detector][0])) == 1


@pytest.mark.parametrize("detector", sorted(CASES))
def test_no_finding_field_can_carry_the_matched_value(detector):
    positive = CASES[detector][0]
    findings = scan_text(positive)
    assert findings
    payload = positive.split("=", 1)[-1].strip()
    for finding in findings:
        for value in vars(finding).values():
            assert payload not in str(value)


def test_every_declared_detector_has_a_case_or_is_the_generic_one():
    """A detector nobody tests is a detector nobody has shown can fire."""
    covered = set(CASES) | {
        DETECTOR_HIGH_ENTROPY_ASSIGNMENT,
        # ops_ is covered by tests/test_message_model_secrets.py
        "op_service_account_token",
    }
    assert set(ALL_DETECTORS) <= covered


def test_the_vendor_set_excludes_the_generic_assignment_detector():
    """The precision split the file scanner depends on. If the generic
    detector ever joins VENDOR_DETECTORS, every audit gets 46 findings of
    noise and the hook starts blocking on `const STORAGE_KEY = ...`."""
    assert DETECTOR_HIGH_ENTROPY_ASSIGNMENT not in VENDOR_DETECTORS


def test_a_bare_pem_header_with_no_key_material_is_not_a_key():
    """Measured false positive: tests/test_config_files.py writes exactly
    this to assert a key-shaped file is flagged sensitive. A header with
    nothing after it is documentation, not a private key."""
    assert scan_text('write_text("-----BEGIN OPENSSH PRIVATE KEY-----\\n")') == []
    assert scan_text(FAKE_PEM), "positive control: a real block still fires"


def test_ops_inside_an_identifier_is_not_a_service_account_token():
    """Measured false positive: this exact function name in
    tests/test_pushover_send.py fired the op detector, because `drops_`
    supplies the prefix and the rest supplies 41 chars of payload."""
    assert scan_text(
        "async def test_router_drops_emit_when_all_three_channels_unconfigured():"
    ) == []


def test_mixed_alphabet_separates_a_payload_from_an_identifier():
    assert has_mixed_alphabet(FAKE_GITHUB)
    assert not has_mixed_alphabet("emit_when_all_three_channels_unconfigured")


def test_is_placeholder_covers_all_three_rejection_reasons():
    assert is_placeholder("op://Claude/Item/field")
    assert is_placeholder("${CF_API_TOKEN}")
    assert is_placeholder("ghp_" + "x" * 36)
    assert is_placeholder("YOUR_TOKEN_HERE_0000000000000000000000")
    assert not is_placeholder(FAKE_GITHUB), "positive control"


def test_narrowing_the_detector_set_narrows_the_findings():
    text = f"GITHUB_TOKEN={FAKE_GITHUB}"
    assert scan_text(text, detectors=[DETECTOR_GITHUB_TOKEN])
    assert scan_text(text, detectors=[DETECTOR_SLACK_TOKEN]) == []


def test_an_unknown_detector_name_is_refused_rather_than_ignored():
    """Silently running zero detectors would return an empty list that
    looks exactly like a clean scan."""
    with pytest.raises(ValueError):
        scan_text("anything", detectors=["not_a_detector"])
