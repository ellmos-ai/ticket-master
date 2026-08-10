#!/usr/bin/env bash
# ticket-master.sh — Unix dispatcher for ticket-master
# Usage: ./bin/ticket-master.sh [--provider claude|codex|agy]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PROVIDER="claude"
if [[ "${TM_PROVIDER:-}" != "" ]]; then
    PROVIDER="${TM_PROVIDER}"
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --provider) PROVIDER="$2"; shift 2 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

PYTHON_BIN="${PYTHON:-python3}"
if ! command -v "${PYTHON_BIN}" &>/dev/null; then
    PYTHON_BIN="python"
fi
if ! command -v "${PYTHON_BIN}" &>/dev/null; then
    echo "ERROR: Python 3 is required to resolve config and launch the provider." >&2
    exit 1
fi

exec "${PYTHON_BIN}" "${SCRIPT_DIR}/ticket_master.py" --provider "${PROVIDER}"
