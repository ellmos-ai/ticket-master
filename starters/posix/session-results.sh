#!/bin/sh
# Usage: session-results.sh /absolute/session.json
set -eu
if [ "$#" -ne 1 ]; then
    echo 'Usage: session-results.sh /absolute/session.json' >&2
    exit 2
fi
exec ticket-master-results open "$1"
