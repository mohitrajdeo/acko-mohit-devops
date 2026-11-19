#!/usr/bin/env bash
# install-and-run.sh — launch control plane in background from launchpad/ directory
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pip3 install --break-system-packages flask >/dev/null 2>&1 || pip install flask
cd "$DIR"
python3 -c "import flask; print('flask', flask.__version__)"
nohup python3 app.py > /tmp/launchpad.log 2>&1 &
sleep 2
tail -5 /tmp/launchpad.log
curl -s -o /dev/null -w "UI http_code: %{http_code}\n" http://localhost:7000/ || true
