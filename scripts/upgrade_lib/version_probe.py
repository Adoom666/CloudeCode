#!/usr/bin/env python3
"""Thin CLI wrapper around src/core/version.py and src/core/update_check.py.

WHY THIS EXISTS: upgrade.sh and rollback.sh both need to answer "what version
is this install at" and "what release tags does the remote have" before they
touch anything destructive. That logic already lives in
``src/core/version.py`` (the single version resolver, THE RULE: a release is
a git tag) and ``src/core/update_check.py`` (remote tag listing). This module
does not reimplement either — it imports them and exposes what a shell
script needs as small, independently testable functions, then a subcommand
CLI on top so bash can call it as a subprocess.

THREE-OUTCOME CONTRACT (see project CLAUDE.md's "three-outcome rule"): every
subcommand exits with one of exactly three codes, and never invents a fourth
meaning by conflating two of them:

    0   pass       - the thing asked for was determined and is printed on stdout
    2   fail       - the thing was determined and the answer is negative
                      (tag not found, version does not match)
    3   could-not-evaluate - it could not be determined at all (offline, no
                      git, malformed repo, remote unreachable). A caller that
                      treats 3 the same as 0 or 2 has reintroduced the bug
                      this module exists to prevent.

Exit code 1 is reserved for command-line usage errors (bad flags), which is
a different category from all three outcomes above.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional


def _install_src_on_path(install_dir: Path) -> None:
    """Make ``src.core.*`` importable from an arbitrary install directory.

    Description: inserts ``install_dir`` at the front of ``sys.path`` so the
      ``src`` package inside THAT checkout is what resolves, not whatever
      ``src`` package (if any) happens to be importable from the caller's
      own cwd. Load-bearing when this script is invoked against a different
      checkout than the one it physically lives in (rollback checks out an
      older tag first, then re-probes against it).
    Inputs:
      install_dir (Path): repository root containing ``src/core/version.py``.
    Output: None. Mutates ``sys.path`` and clears any previously-imported
      ``src.core.version`` / ``src.core.update_check`` modules so a second
      call against a different install_dir in the same process cannot serve
      stale cached code.
    """
    resolved = str(install_dir.resolve())
    if resolved in sys.path:
        sys.path.remove(resolved)
    sys.path.insert(0, resolved)
    for name in list(sys.modules):
        if name == "src" or name.startswith("src."):
            del sys.modules[name]


def current_version(install_dir: Path) -> str:
    """Resolve the version this install would report right now.

    Description: delegates entirely to ``src.core.version.resolve_version``
      run against ``install_dir`` — no independent version logic here.
    Inputs:
      install_dir (Path): repository root to resolve against.
    Output: str - the resolved version, or "" when it could not be
      determined (the resolver's own documented "give up honestly" case).
    Example: current_version(Path("/opt/cloudecode")) -> "0.8.2"
    """
    _install_src_on_path(install_dir)
    from src.core.version import resolve_version  # noqa: E402 (path just set)

    return resolve_version(install_dir)


def resolve_remote(install_dir: Path, config_path: Optional[Path]) -> str:
    """Decide which git remote to consult for release tags.

    Description: same precedence ``update_check.UpdateChecker.resolve_remote``
      uses (configured ``updates.remote`` in config.json, else the checkout's
      own ``origin``, else the public fallback), reimplemented as a plain
      function here because the class version is wired to a background
      thread and a cache file this CLI has no use for.
    Inputs:
      install_dir (Path): repository root.
      config_path (Path | None): path to config.json; None or missing is
        treated as "no configured remote" rather than an error.
    Output: str - the remote URL to use.
    """
    _install_src_on_path(install_dir)
    from src.core.update_check import (  # noqa: E402
        FALLBACK_REMOTE,
        discover_origin_remote,
        read_configured_remote,
    )

    configured = read_configured_remote(config_path) if config_path else ""
    return configured or discover_origin_remote(install_dir) or FALLBACK_REMOTE


def list_release_tags(install_dir: Path, remote: str) -> list[str]:
    """List plain release tags on ``remote``, oldest first.

    Description: delegates to ``src.core.update_check.fetch_remote_tags``.
      Raises rather than returning an empty list on failure so the CLI layer
      can tell "no tags published" (empty list, a FAIL) apart from
      "could not reach the remote at all" (an exception, COULD-NOT-EVALUATE).
    Inputs:
      install_dir (Path): repository root (only used to import the module).
      remote (str): git remote URL.
    Output: list[str] - release tags such as ["0.8.0", "0.8.1", "0.8.2"].
    Raises:
      src.core.update_check.UpdateCheckError: remote unreachable, git
        missing, or the call timed out.
    """
    _install_src_on_path(install_dir)
    from src.core.update_check import fetch_remote_tags  # noqa: E402

    return fetch_remote_tags(remote)


def _cmd_current(args: argparse.Namespace) -> int:
    """Description: print the resolved version or fail with COULD-NOT-EVALUATE.

    Inputs: args (argparse.Namespace) with ``install_dir``.
    Output: int exit code - 0 with version on stdout, 3 with a reason on
      stderr and nothing on stdout.
    """
    version = current_version(Path(args.install_dir))
    if not version:
        print(
            "could not determine the installed version "
            f"(no CLOUDE_APP_VERSION, no VERSION file, no exact git tag, "
            f"no macOS/package.json, no git describe) in {args.install_dir}",
            file=sys.stderr,
        )
        return 3
    print(version)
    return 0


def _cmd_remote(args: argparse.Namespace) -> int:
    """Description: print the remote that would be consulted for tags.

    Inputs: args (argparse.Namespace) with ``install_dir`` and optional
      ``config``.
    Output: int - always 0; there is no failure mode, only fallbacks.
    """
    config_path = Path(args.config) if args.config else None
    print(resolve_remote(Path(args.install_dir), config_path))
    return 0


def _cmd_tags(args: argparse.Namespace) -> int:
    """Description: print every release tag on the remote, one per line.

    Inputs: args (argparse.Namespace) with ``install_dir`` and ``remote``.
    Output: int - 0 with tags on stdout (possibly zero lines, meaning FAIL
      "no tags published" is left for the caller to interpret), 3 with a
      reason on stderr when the remote could not be reached at all.
    """
    try:
        tags = list_release_tags(Path(args.install_dir), args.remote)
    except Exception as exc:  # the module's own UpdateCheckError, imported lazily
        print(f"could not list release tags from {args.remote}: {exc}", file=sys.stderr)
        return 3
    for tag in tags:
        print(tag)
    return 0


def _cmd_latest(args: argparse.Namespace) -> int:
    """Description: print the newest release tag on the remote.

    Inputs: args (argparse.Namespace) with ``install_dir`` and ``remote``.
    Output: int - 0 with the tag on stdout, 2 if the remote answered but
      published no release tags, 3 if the remote could not be reached.
    """
    try:
        tags = list_release_tags(Path(args.install_dir), args.remote)
    except Exception as exc:
        print(f"could not list release tags from {args.remote}: {exc}", file=sys.stderr)
        return 3
    if not tags:
        print(f"{args.remote} published no release tags", file=sys.stderr)
        return 2
    print(tags[-1])  # fetch_remote_tags returns sorted-by-version, newest last
    return 0


def _cmd_check_tag(args: argparse.Namespace) -> int:
    """Description: verify one exact tag exists among the remote's release tags.

    Inputs: args (argparse.Namespace) with ``install_dir``, ``remote``,
      ``tag``.
    Output: int - 0 if the tag exists, 2 if the remote answered but the tag
      is not in the list, 3 if the remote could not be reached.
    """
    try:
        tags = list_release_tags(Path(args.install_dir), args.remote)
    except Exception as exc:
        print(f"could not list release tags from {args.remote}: {exc}", file=sys.stderr)
        return 3
    if args.tag not in tags:
        print(
            f"{args.tag} is not a published release tag on {args.remote}. "
            f"Known tags: {', '.join(tags) if tags else '(none)'}",
            file=sys.stderr,
        )
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Description: construct the argparse CLI shared by all subcommands.

    Inputs: none.
    Output: argparse.ArgumentParser fully configured with subcommands
      ``current``, ``remote``, ``tags``, ``latest``, ``check-tag``.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_current = sub.add_parser("current", help="print the resolved installed version")
    p_current.add_argument("--install-dir", required=True)
    p_current.set_defaults(func=_cmd_current)

    p_remote = sub.add_parser("remote", help="print the git remote used for release tags")
    p_remote.add_argument("--install-dir", required=True)
    p_remote.add_argument("--config", default=None, help="path to config.json, optional")
    p_remote.set_defaults(func=_cmd_remote)

    p_tags = sub.add_parser("tags", help="list every release tag on the remote")
    p_tags.add_argument("--install-dir", required=True)
    p_tags.add_argument("--remote", required=True)
    p_tags.set_defaults(func=_cmd_tags)

    p_latest = sub.add_parser("latest", help="print the newest release tag on the remote")
    p_latest.add_argument("--install-dir", required=True)
    p_latest.add_argument("--remote", required=True)
    p_latest.set_defaults(func=_cmd_latest)

    p_check = sub.add_parser("check-tag", help="verify a tag is a published release tag")
    p_check.add_argument("--install-dir", required=True)
    p_check.add_argument("--remote", required=True)
    p_check.add_argument("tag")
    p_check.set_defaults(func=_cmd_check_tag)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """Description: CLI entry point.

    Inputs: argv (list[str] | None) - defaults to sys.argv[1:].
    Output: int exit code, per each subcommand's contract above.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
