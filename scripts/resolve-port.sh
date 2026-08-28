#!/bin/bash
# scripts/resolve-port.sh
#
# Single shared shell-side resolver for the server's configured port.
# Sourced by every shell script that needs to find, probe, or kill the
# Cloude Code server by port (nuke.sh, stop.sh, reset.sh at the repo root,
# and scripts/upgrade_lib/upgrade_rollback_common.sh for upgrade/rollback
# verification). Never executed directly.
#
# The single configuration root for the port is src/config.py's
# ``Settings.port: int = 8000`` (pydantic-settings, loaded from an
# install's .env). This file cannot import that Python class without a
# working venv, so it re-implements the same two-step lookup pydantic
# performs (env override, else field default) by reading .env directly.
# DEFAULT_PORT below is the ONLY place in the shell-side tooling that
# spells out "8000" - every script that needs a port sources this file
# and calls resolve_port instead of hardcoding the literal.
#
# Three-outcome contract (this repo's governing standard - see CLAUDE.md
# "THE THREE-OUTCOME RULE"):
#   - .env absent, or present with no PORT= line -> DEFAULT_PORT on
#     stdout, return 0. This is a real, fully-determined answer ("no
#     override configured"), identical to what Settings.port's own field
#     default resolves to inside the server - not a guess.
#   - PORT= present and a valid 1-65535 integer -> that value on stdout,
#     return 0.
#   - PORT= present but not a valid integer -> nothing on stdout, a
#     "could not determine port" message on stderr, return 1. Callers
#     MUST check the exit status and fail loud themselves - this
#     function never falls back to DEFAULT_PORT on that path. Silently
#     using DEFAULT_PORT there is exactly the false-green class this
#     project has been burned by repeatedly (see CLAUDE.md hazard list).

# Default port. Matches src/config.py's Settings.port field default.
# Used ONLY when .env has no PORT= override - the same case in which
# Settings.port itself would resolve to this value on the Python side.
DEFAULT_PORT=8000

# Description: resolve the configured server port for an install
#   directory by reading PORT= out of its .env, the same file
#   pydantic-settings loads (src/config.py). See the three-outcome
#   contract above.
# Inputs: $1 - install_dir (directory containing .env; may be absent).
# Output: prints the resolved port on stdout and returns 0, OR prints
#   nothing on stdout, prints a reason to stderr, and returns 1 when
#   PORT= is present but not a valid port number.
resolve_port() {
    local install_dir="${1:-.}"
    local env_file="${install_dir}/.env"

    if [ ! -f "${env_file}" ]; then
        printf '%s\n' "${DEFAULT_PORT}"
        return 0
    fi

    local raw
    raw="$(grep -E '^PORT=' "${env_file}" | tail -1 | cut -d= -f2- | tr -d '[:space:]' | tr -d "'\"")"

    if [ -z "${raw}" ]; then
        printf '%s\n' "${DEFAULT_PORT}"
        return 0
    fi

    case "${raw}" in
        ''|*[!0-9]*)
            printf 'could not determine port: PORT=%s in %s is not a valid port number\n' "${raw}" "${env_file}" >&2
            return 1
            ;;
    esac

    if [ "${raw}" -lt 1 ] || [ "${raw}" -gt 65535 ]; then
        printf 'could not determine port: PORT=%s in %s is out of range (1-65535)\n' "${raw}" "${env_file}" >&2
        return 1
    fi

    printf '%s\n' "${raw}"
    return 0
}
