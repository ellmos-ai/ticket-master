#!/usr/bin/env bash
# start-claude.sh - launch ticket-master with Claude
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/../ticket-master.sh" --provider claude "$@"
