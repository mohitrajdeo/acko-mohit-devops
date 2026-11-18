#!/usr/bin/env bash
# install flask in WSL (PEP 668 workaround), verify, then launch control plane in background
set -x
pip3 install --break-system-packages --ignore-installed blinker flask 2>&1 | tail -1
python3 -c "import flask; print('flask', flask.__version__)"
cd /root/acko-mohit-devops/launchpad
nohup python3 app.py > /tmp/launchpad.log 2>&1 &
sleep 3
tail -5 /tmp/launchpad.log
curl -s -o /dev/null -w "UI http_code: %{http_code}\n" http://localhost:7000/
