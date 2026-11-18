#!/usr/bin/env bash
# Smoke test: register -> build good -> deploy -> deploy broken -> check auto-rollback
set -x
KIT=/root/acko-mohit-devops/devops-startup-kit/launchpad-starter-kit/launchpad-starter-kit/services/quote-svc

curl -s -X POST localhost:7000/api/services -H 'Content-Type: application/json' \
  -d "{\"name\":\"quote-svc\",\"repo_path\":\"$KIT\",\"port\":8080}"
echo

curl -s -X POST localhost:7000/api/services/1/build -H 'Content-Type: application/json' -d '{"version":"v1.1.0"}'
echo

curl -s -X POST localhost:7000/api/services/1/deploy -H 'Content-Type: application/json' -d '{"version":"v1.1.0","environment":"staging"}'
echo

sleep 25   # health gate: 3 probes x 5s
curl -s localhost:7000/api/deploys
echo

# broken version — v1.2.0 of quote-svc (per kit README one version of each is broken)
curl -s -X POST localhost:7000/api/services/1/deploy -H 'Content-Type: application/json' -d '{"version":"v1.2.0","environment":"staging"}'
echo

sleep 25
curl -s localhost:7000/api/deploys
echo
curl -s localhost:7000/api/events | head -c 2000
