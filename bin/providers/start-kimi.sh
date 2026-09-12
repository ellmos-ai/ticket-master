#!/usr/bin/env bash
# start-kimi.sh - launch ticket-master with Kimi
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/../ticket-master.sh" --provider kimi "$@"
