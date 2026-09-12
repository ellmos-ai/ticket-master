#!/usr/bin/env bash
# start-agy.sh - launch ticket-master with agy (Gemini)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/../ticket-master.sh" --provider agy "$@"
