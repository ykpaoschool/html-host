#!/bin/bash
set -e

cd "$(dirname "$0")"

# Install dependencies if needed
if [ ! -d "venv" ]; then
    python3 -m venv venv
    venv/bin/pip install -r requirements.txt
fi

# Run with gunicorn in production, flask dev server otherwise
if [ "$1" = "prod" ]; then
    # Prod is expected to sit behind TLS, so mark the session cookie Secure
    # by default. Set SESSION_COOKIE_SECURE=false for a plain-http LAN deploy.
    export SESSION_COOKIE_SECURE="${SESSION_COOKIE_SECURE:-true}"
    venv/bin/gunicorn -w 2 -b 0.0.0.0:5000 "app:app"
else
    # Dev: an ephemeral random key satisfies the insecure-SECRET_KEY guard
    # without forcing every dev to export one (sessions reset each run).
    export SECRET_KEY="${SECRET_KEY:-$(venv/bin/python -c 'import secrets; print(secrets.token_hex(32))')}"
    venv/bin/python app.py
fi
