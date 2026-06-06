#!/usr/bin/env bash
# timeguard.sh — wrapper to run any command under a hard wall-clock ceiling.
#
# Usage:
#   timeguard.sh [SECONDS] [--] command [args...]
#   timeguard.sh command [args...]          (default: 300s ceiling)
#
# Can also be sourced to add `timeguard` shell function:
#   source scripts/timeguard.sh && timeguard 120 make test

set -euo pipefail

CEILING="${TIMEGUARD_CEILING:-300}"
declare -a CMD=()

# Parse optional leading timeout value
if [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]]; then
    CEILING="$1"
    shift
fi
[[ $# -gt 0 && "$1" == "--" ]] && shift

if [[ $# -eq 0 ]]; then
    echo "timeguard: missing command" >&2
    exit 1
fi

exec timeout --kill-after=5 "$CEILING" "$@"
