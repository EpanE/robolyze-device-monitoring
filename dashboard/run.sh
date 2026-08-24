#!/usr/bin/env bash
# Deliberately NOT `set -e`. This script runs in a truck, sometimes with no internet, and
# a launcher that refuses to start a perfectly working dashboard because pip could not
# reach PyPI is worse than one that starts with slightly stale dependencies.
set -uo pipefail
cd "$(dirname "$0")"

PY=".venv/bin/python"

if [ ! -f "$PY" ]; then
    echo "Creating virtual environment..."
    if ! python3 -m venv .venv; then
        echo "ERROR: could not create .venv — is python3 installed?" >&2
        exit 1
    fi
fi

# Skipped entirely when the pinned set is already satisfied, so the normal start needs no
# network at all. `pip check`-style verification via a dry run is slow; importing the four
# top-level packages is instant and answers the only question that matters: can this run?
if ! "$PY" -c "import flask, paramiko, requests, waitress" 2>/dev/null; then
    echo "Installing dependencies..."
    if ! "$PY" -m pip install -q -r requirements.txt; then
        echo "WARNING: dependency install failed (offline?). Starting anyway — if the" >&2
        echo "         dashboard errors on startup, reconnect and re-run this script." >&2
    fi
fi

"$PY" app.py &
SERVER_PID=$!

sleep 2

# Read the port from the same .env the app reads, rather than assuming 5000.
PORT="$(sed -n 's/^[[:space:]]*PORT[[:space:]]*=[[:space:]]*["'"'"']\{0,1\}\([0-9]\{1,\}\).*/\1/p' ../.env 2>/dev/null | tail -1)"
URL="http://127.0.0.1:${PORT:-5000}/"

if command -v open >/dev/null 2>&1; then
    open "$URL"
elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL"
else
    echo "Open this link in your browser: $URL"
fi

wait "$SERVER_PID"
