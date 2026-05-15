#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/backend"
PY="${PYTHON:-python3.8}"
if [ ! -d .venv ]; then
  $PY -m venv .venv
  .venv/bin/pip install -U pip
  .venv/bin/pip install -r requirements.txt
fi
exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
