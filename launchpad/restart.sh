#!/usr/bin/env bash
# restart control plane with latest app.py (kept alive after shell exits)
cd /root/acko-mohit-devops/launchpad
pkill -f "python3 app.py" 2>/dev/null
sleep 1
setsid nohup python3 app.py > /tmp/launchpad.log 2>&1 < /dev/null &
disown
sleep 2
curl -s -o /dev/null -w "UI: %{http_code}\n" localhost:7000/
