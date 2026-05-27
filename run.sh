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
    venv/bin/gunicorn -w 2 -b 0.0.0.0:5000 "app:app"
else
    venv/bin/python app.py
fi
