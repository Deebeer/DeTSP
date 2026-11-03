#!/bin/sh
# DeTSP macOS/Linux launcher: create venv on first run, install deps, start.
set -e
cd "$(dirname "$0")"
PY=$(command -v python3 || true)
if [ -z "$PY" ]; then
  echo "Python 3.11+ not found. Install it first (https://www.python.org)."
  exit 1
fi
if [ ! -d .venv ]; then
  echo "First run: creating virtual environment..."
  "$PY" -m venv .venv
fi
echo "Installing dependencies..."
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -e ".[live]"
[ -f .env ] || cp .env.example .env
[ -f config.yaml ] || cp config.example.yaml config.yaml
echo "Starting DeTSP (panel: http://127.0.0.1:8320/) ..."
exec .venv/bin/python -m detsp
