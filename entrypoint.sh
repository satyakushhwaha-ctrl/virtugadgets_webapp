#!/bin/sh
set -eu

cd /app

python manage.py migrate
python manage.py collectstatic --noinput
python manage.py provision_admin

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

exec gunicorn config.wsgi:application --bind "0.0.0.0:${PORT:-8000}" --timeout 180
