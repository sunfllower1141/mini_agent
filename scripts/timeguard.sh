#!/usr/bin/env bash
# timeguard.sh — wrapper to run any command under a hard wall-clock ceiling.
#
# Usage:
#   timeguard.sh [SECONDS] [--] command [args...]
#   timeguard.sh command [args...]          (default: 300s ceiling)
#
# Can also be sourced to add `timeguard` shell function:
#   source scripts/timeguard.sh && timeguard 120 make test
#
# Portable: tries GNU timeout, then gtimeout (macOS coreutils), then a
# pure-bash background-process fallback.

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

# ── portable timeout dispatch ──────────────────────────────────────────

if command -v timeout &>/dev/null; then
    # Linux / GNU coreutils
    exec timeout --kill-after=5 "$CEILING" "$@"
elif command -v gtimeout &>/dev/null; then
    # macOS with `brew install coreutils`
    exec gtimeout --kill-after=5 "$CEILING" "$@"
else
    # Pure-bash fallback for macOS (no coreutils).
    # Runs the command in the background, waits with a ceiling, and sends
    # SIGTERM (then SIGKILL after a 5s grace) if the ceiling is exceeded.
    "$@" &
    PID=$!

    # Watcher: fires after CEILING seconds
    (
        sleep "$CEILING"
        kill -TERM "$PID" 2>/dev/null || true
        sleep 5
        kill -KILL "$PID" 2>/dev/null || true
    ) &
    WATCHER=$!

    set +e
    wait "$PID" 2>/dev/null
    RC=$?
    set -e

    # Clean up the watcher
    kill -KILL "$WATCHER" 2>/dev/null || true
    wait "$WATCHER" 2>/dev/null || true

    exit "$RC"
fi