"""Capturing a machine's identity, and proving a corpus came from it.

TWO JOBS, DELIBERATELY SEPARATED. ``capture_identity`` answers "which
machine am I", and it can only be trusted when it runs ON that machine.
``build_manifest`` answers "what files were here, and what did they
hash to", and it has the same restriction. Everything downstream of
those two functions is a COMPARISON, which can run anywhere.

WHY A MANIFEST IS THE WHOLE POINT. Nothing inside a .jsonl transcript
names the machine that wrote it. So an ingester reading a directory has
no evidence of provenance at all: it can only assert "these are the
mini's files because I copied them from the mini", and that assertion
cannot be checked, which makes it a verification step that can never
fail. The manifest moves the evidence to where it can be checked - the
source machine states its platform uuid and one sha256 per file, and
the ingester compares against bytes it actually read. A file that is
absent from the manifest, or present with a different hash, is
``cannot_determine``. It is still STORED; only its ATTRIBUTION is
withheld.

WHAT THIS DOES NOT CLAIM. A manifest proves the bytes match what the
source machine reported at collection time. It does not prove those
bytes ORIGINATED there - a session file copied onto the mini last week
is genuinely on the mini and will verify as the mini's, correctly,
because that is what "this corpus contains this file" means. The
cross-host session measurement (see message_host_dimension) is what
surfaces the copying, and it is a report, not a gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple

#: How a machine_id was obtained. ``platform_uuid`` is IOPlatformUUID
#: read from IORegistry on that machine; ``declared`` is an operator
#: string, which is a weaker fact and says so.
SCHEME_PLATFORM_UUID: str = "platform_uuid"
SCHEME_DECLARED: str = "declared"

#: The three attribution outcomes, mirroring
#: message_host_ddl.HOST_ATTRIBUTION_VALUES.
ATTR_VERIFIED: str = "manifest_verified"
ATTR_DECLARED: str = "declared"
ATTR_CANNOT_DETERMINE: str = "cannot_determine"

_UUID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)

_CHUNK = 1 << 20


@dataclass(frozen=True)
class HostIdentity:
    """One machine's identity as captured on that machine.

    - ``machine_id``: the platform uuid, or an operator string.
    - ``machine_id_scheme``: which of the two the id is.
    - ``display_name``: the human name (macOS ComputerName).
    - ``hostname``: whatever ``hostname`` reported. Descriptive only.
    - ``platform``: OS and release, for the reader's benefit.
    """

    machine_id: str
    machine_id_scheme: str
    display_name: str
    hostname: str
    platform: str


def _ioreg_platform_uuid() -> Optional[str]:
    """The machine's IOPlatformUUID, or None if it cannot be read.

    Description: shells out to ``ioreg`` because there is no stdlib
      route to IORegistry. Returns None rather than raising or inventing
      a value - an unreadable identity is a third outcome, and the
      caller records it as ``declared`` instead of pretending.
    Inputs: none.
    Output: str uuid, or None.
    Example: _ioreg_platform_uuid() is None or len(_ioreg_platform_uuid()) == 36
    """
    if platform.system() != "Darwin":
        return None
    try:
        out = subprocess.run(
            ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True, text=True, timeout=20, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
    if match is None:
        return None
    value = match.group(1).strip()
    return value if _UUID_RE.match(value) else None


def _scutil(key: str) -> str:
    """One ``scutil --get`` value, or the empty string.

    Description: descriptive metadata only. A failure here is never an
      error, because nothing keys off these values.
    Inputs: key (str) - e.g. "ComputerName".
    Output: str, possibly empty.
    Example: isinstance(_scutil("ComputerName"), str) -> True
    """
    if platform.system() != "Darwin":
        return ""
    try:
        return subprocess.run(
            ["/usr/sbin/scutil", "--get", key],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def capture_identity(declared_id: Optional[str] = None) -> HostIdentity:
    """This machine's identity, read from this machine.

    Description: prefers the platform uuid. Falls back to ``declared_id``
      only when the uuid is unreadable, and marks the scheme so the
      difference in evidence quality survives into the database. MUST be
      run on the machine being identified; run anywhere else it
      truthfully describes the wrong machine.
    Inputs: declared_id (str or None) - fallback identity.
    Output: HostIdentity.
    Raises: ValueError - no platform uuid and no declared_id, so there is
      no identity at all and inventing one is not on the menu.
    Example: capture_identity("x").machine_id_scheme in
      ("platform_uuid", "declared") -> True
    """
    uuid = _ioreg_platform_uuid()
    if uuid is not None:
        machine_id, scheme = uuid, SCHEME_PLATFORM_UUID
    elif declared_id:
        machine_id, scheme = declared_id, SCHEME_DECLARED
    else:
        raise ValueError(
            "no IOPlatformUUID could be read and no declared_id was given - "
            "there is no host identity to record, and inventing one is the "
            "silent wrong attribution this module exists to prevent"
        )
    name = _scutil("ComputerName") or platform.node()
    return HostIdentity(
        machine_id=machine_id, machine_id_scheme=scheme,
        display_name=name or machine_id, hostname=platform.node(),
        platform=f"{platform.system()} {platform.release()}",
    )


def sha256_file(path: str) -> str:
    """sha256 of a file's bytes, read in chunks.

    Description: chunked because the largest transcript in this corpus
      measured 233 MB and a whole-file read is not an option at 19,540
      files.
    Inputs: path (str).
    Output: str - lowercase hex digest.
    Raises: OSError - unreadable.
    Example: len(sha256_file("/dev/null")) -> 64
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def walk_jsonl(root: str) -> List[str]:
    """Every .jsonl path under a root, relative to it, sorted.

    Description: sorted for determinism, and relative because a path
      relative to its corpus root is the only form that means the same
      thing on two machines.
    Inputs: root (str).
    Output: list[str] - relative paths.
    Example: walk_jsonl("/nonexistent") -> []
    """
    found: List[str] = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.endswith(".jsonl"):
                full = os.path.join(dirpath, name)
                found.append(os.path.relpath(full, root))
    found.sort()
    return found


def build_manifest(
    root: str, identity: HostIdentity, corpus_key: str,
) -> Dict[str, object]:
    """Hash every .jsonl under a root and stamp it with this machine's id.

    Description: the evidence a later ingest compares against. Produced
      ON the source machine; produced anywhere else it is worthless, and
      the identity it carries is what makes that checkable.
      An unreadable file is recorded with a null sha and its error, not
      omitted - an absent entry and a failed entry mean different things
      downstream.
    Inputs: root (str), identity (HostIdentity), corpus_key (str).
    Output: dict - json-serialisable manifest.
    Example: build_manifest("/nonexistent", ident, "k")["file_count"] -> 0
    """
    entries: Dict[str, object] = {}
    errors = 0
    for rel in walk_jsonl(root):
        full = os.path.join(root, rel)
        try:
            entries[rel] = {"size": os.path.getsize(full),
                            "sha256": sha256_file(full)}
        except OSError as exc:
            errors += 1
            entries[rel] = {"size": None, "sha256": None,
                            "error": f"{type(exc).__name__}: {exc}"}
    return {
        "machine_id": identity.machine_id,
        "machine_id_scheme": identity.machine_id_scheme,
        "display_name": identity.display_name,
        "hostname": identity.hostname,
        "platform": identity.platform,
        "corpus_key": corpus_key,
        "root_path": root,
        "file_count": len(entries),
        "unreadable_count": errors,
        "files": entries,
    }


def manifest_sha(manifest: Dict[str, object]) -> str:
    """A stable hash of a manifest, for recording which one was used.

    Description: canonical (sorted-key) JSON so the same manifest hashes
      the same regardless of dict ordering.
    Inputs: manifest (dict).
    Output: str - hex digest.
    Example: len(manifest_sha({})) -> 64
    """
    blob = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def classify_attribution(
    manifest: Optional[Dict[str, object]], source_path: str,
    observed_sha256: str, observed_bytes: int,
) -> Tuple[str, str]:
    """Decide how well a file's host attribution is evidenced.

    Description: the three-outcome rule applied to provenance. With no
      manifest the answer is ``declared`` - the operator's claim, stored
      AS a claim. With a manifest, the file's own bytes must hash to
      what the source machine reported, or the attribution is withheld.
      A hash miss is NEVER narrowed to ``declared``; a growing live file
      is the one case that gets a named, separate reason and it still
      does not become ``manifest_verified``.
    Inputs: manifest (dict or None), source_path (str - relative to the
      corpus root), observed_sha256 (str), observed_bytes (int).
    Output: (attribution, detail) - attribution is one of ATTR_VERIFIED,
      ATTR_DECLARED, ATTR_CANNOT_DETERMINE.
    Example: classify_attribution(None, "a.jsonl", "x", 1)[0] -> "declared"
    """
    if manifest is None:
        return ATTR_DECLARED, "no collection manifest for this corpus"
    files = manifest.get("files")
    if not isinstance(files, dict):
        return ATTR_CANNOT_DETERMINE, "manifest has no files table"
    entry = files.get(source_path)
    if entry is None:
        return ATTR_CANNOT_DETERMINE, (
            "not present in the source machine's manifest, so nothing on "
            "that machine attests to this path"
        )
    if not isinstance(entry, dict) or entry.get("sha256") is None:
        return ATTR_CANNOT_DETERMINE, (
            f"the source machine could not hash this file: "
            f"{entry.get('error') if isinstance(entry, dict) else entry!r}"
        )
    if entry["sha256"] == observed_sha256:
        return ATTR_VERIFIED, ""
    size = entry.get("size")
    if isinstance(size, int) and observed_bytes > size:
        return ATTR_CANNOT_DETERMINE, (
            f"bytes read ({observed_bytes}) exceed the manifest size "
            f"({size}) - the file grew after collection, so the manifest "
            "cannot attest to what was read"
        )
    return ATTR_CANNOT_DETERMINE, (
        f"sha256 disagrees with the source machine's manifest "
        f"(read {observed_sha256[:16]}..., manifest "
        f"{str(entry['sha256'])[:16]}...)"
    )


def iter_manifest_paths(manifest: Dict[str, object]) -> Iterator[str]:
    """Every relative path a manifest names, sorted.

    Description: lets a caller antijoin the manifest against what it
      actually ingested, so a file the source machine had and the
      ingester never saw is a NAMED absence rather than an invisible one.
      That direction of the join is the one nothing ever runs.
    Inputs: manifest (dict).
    Output: iterator of str.
    Example: list(iter_manifest_paths({"files": {"b": {}, "a": {}}}))
      -> ["a", "b"]
    """
    files = manifest.get("files")
    if not isinstance(files, dict):
        return iter(())
    return iter(sorted(files))


def _main(argv: List[str]) -> int:
    """Print a collection manifest for one corpus root, as JSON, to stdout.

    Description: this module is deliberately dependency-free (stdlib
      only, no src.core imports) so that it can be executed ON another
      machine by piping the file itself into that machine's python -
      ``ssh host '/usr/bin/python3 - <root> <corpus_key>' < this_file``.
      That is what makes the manifest evidence generated on the SOURCE
      machine rather than an assertion made by the reader, and it writes
      nothing to that machine's disk: the program arrives on stdin and
      the manifest leaves on stdout.
    Inputs: argv (list of str) - [root, corpus_key, optional declared_id].
    Output: int exit code.
    Example: _main(["/nonexistent", "k"]) -> 0
    """
    if len(argv) < 2:
        print("usage: <root> <corpus_key> [declared_id]", flush=True)
        return 2
    root = os.path.expanduser(argv[0])
    identity = capture_identity(argv[2] if len(argv) > 2 else None)
    manifest = build_manifest(root, identity, argv[1])
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    import sys as _sys

    raise SystemExit(_main(_sys.argv[1:]))
