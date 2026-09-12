#!/usr/bin/env bash
# start-codex.sh - launch ticket-master with Codex
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/../ticket-master.sh" --provider codex "$@"
