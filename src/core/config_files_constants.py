"""
Constants for the config/project file tree + editor feature.

Split out of ``src/core/config_files.py`` purely to keep that module under
this project's 500-line-per-file rule; the two together are one feature.
``config_files.py`` is the only importer and re-exports nothing extra -
every list/read/write call there re-derives its answer from the constants
below, so this file is the single source of truth for "what does this
feature show, and how" (see config_files.py's module docstring for the
full security posture these constants implement).
"""
from __future__ import annotations

# ---- single source of truth: what this feature shows -----------------

ALLOWED_TOP_LEVEL_FILES = frozenset({
    "CLAUDE.md",
    "CONFIGURATION_NOTES.md",
    "settings.json",
    "settings.local.json",
    # A specific, known, named file (unlike .env*/id_rsa/*.pem, which are
    # pattern-matched, not fixed names) - sensitive (SENSITIVE_NAMES) but
    # no longer hidden, per the 2026-08 policy reversal: this app already
    # grants a full remote shell, so refusing this one file server-side
    # was access-control theater, not real protection. It IS masked
    # on-screen until the user explicitly reveals it.
    ".credentials.json",
})

ALLOWED_TOP_LEVEL_DIRS = frozenset({
    "hooks",
    "rules",
    "commands",
    "skills",
    "agents",
    "scheduled-tasks",
    "scripts",
    "_scripts",
    "superclaude",
})

# 1075 files / 41MB on record - shown, but collapsed by default and
# read-only. Not part of ALLOWED_TOP_LEVEL_DIRS's "expand freely" set.
READONLY_COLLAPSED_DIRS = frozenset({"plugins"})

# Roots that skip the ALLOWED_TOP_LEVEL_* allow-list entirely - a real
# project directory has no fixed shape, so "workdir" is a hide-list-only
# (blocklist) root rather than an allow-list one. "user" and "project"
# stay allow-listed: they are this app's OWN config surface.
BLOCKLIST_ONLY_ROOTS = frozenset({"workdir"})

# Exact names hidden outright - churn/state, VCS internals, and huge
# dependency/build trees, none of which are "config" or worth ever
# rendering. Enforced at the listing layer AND at resolve_safe_path() so a
# client can't reach them by guessing the relative path either. NOTE: this
# is a "never shown, never resolvable" list - distinct from SENSITIVE_*
# below, which IS shown/resolvable but masked on-screen.
HIDE_NAMES = frozenset({
    "projects",
    "backups",
    "cache",
    "debug",
    "paste-cache",
    "session-env",
    "statsig",
    "todos",
    "shell-snapshots",
    "file-history",
    "history.jsonl",
    "stats-cache.json",
    # VCS internals - noise + a stale/mid-operation .git can corrupt if
    # poked at through an editor; this is about integrity, not secrecy.
    ".git",
    # Huge, regenerable dependency/build trees a project is likely to
    # contain. Hiding outright (rather than merely collapsing) means the
    # server never even walks them, so a project with a multi-gigabyte
    # node_modules costs this feature nothing.
    "node_modules",
    "venv",
    ".venv",
    "dist",
    "build",
    "__pycache__",
})

# Prefix match (not exact) - catches .env, .env.local, .env.production, etc.
# These are SENSITIVE, not hidden - see SENSITIVE_PREFIXES below. Kept as a
# separate constant name for HIDE_PREFIXES since nothing currently needs a
# true hidden-by-prefix rule; retained for the shape of the API.
HIDE_PREFIXES = ()

# ---- sensitive-but-visible: shown + resolvable, masked on-screen -------
#
# These files are NOT refused. Refusing them here while a full remote
# shell can `cat` the same file two panels over is inconsistent access
# control theater, not real security. What actually matters is accidental
# ON-SCREEN exposure (screenshots, screen-share, shoulder-surfing) and
# this feature ENUMERATING their existence for someone who didn't already
# know to look - so they are flagged `is_sensitive` and the client masks
# their content until an explicit, named reveal
# (App.showConfirmModal-gated). A write additionally requires
# `acknowledge_sensitive=True`, mirroring the executable-write gate.
SENSITIVE_NAMES = frozenset({
    ".credentials.json",
    "id_rsa",
    "id_ed25519",
})
SENSITIVE_PREFIXES = (".env",)
SENSITIVE_SUFFIXES = (".pem", ".key")

# Executable file types under hooks/ and scripts/ - code Claude Code RUNS
# automatically, so these get extra handling (marker, mandatory backup -
# already true for everything, mandatory confirm).
EXECUTABLE_EXTENSIONS = frozenset({".py", ".cjs", ".js", ".sh"})
EXECUTABLE_DIRS = frozenset({"hooks", "scripts"})

MAX_READ_BYTES = 2 * 1024 * 1024  # 2MB - a config file bigger than this is not what this editor is for.
