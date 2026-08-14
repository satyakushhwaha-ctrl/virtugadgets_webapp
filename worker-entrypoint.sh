#!/bin/sh
set -eu

# Search tasks use non-headless Chromium. The web entrypoint starts Xvfb for
# Gunicorn-side tooling, but a Railway worker bypasses that entrypoint.
Xvfb :99 -screen 0 1440x900x24 -nolisten tcp &
XVFB_PID=$!

cleanup() {
    if kill -0 "$XVFB_PID" 2>/dev/null; then
        kill "$XVFB_PID" 2>/dev/null || true
        wait "$XVFB_PID" 2>/dev/null || true
    fi
}

trap cleanup EXIT INT TERM

export DISPLAY=:99

exec "$@"
