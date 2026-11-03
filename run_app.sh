#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11 or newer is required"'
if ! command -v claude >/dev/null 2>&1; then
  echo "Claude Code is missing. Use the Cloud Shell environment from Assignment 4."
  exit 1
fi
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install --quiet -r requirements.txt
echo "Open Cloud Shell Web Preview, then Preview on port 8080."
exec .venv/bin/python -m streamlit run app.py \
  --server.port=8080 --server.address=0.0.0.0 --server.headless=true \
  --browser.serverAddress=localhost --browser.gatherUsageStats=false \
  --server.enableCORS=false --server.enableXsrfProtection=false
