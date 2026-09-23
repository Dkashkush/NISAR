#!/usr/bin/env bash
# Start the NISAR vs Sentinel-1 app (macOS / Linux). First run installs everything (a few minutes).
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Setting up Python environment, please wait..."
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -e ".[app]"
fi
exec .venv/bin/sarcompare gui
