#!/usr/bin/env bash
# Launchpad acceptance test — the whole story in one script.
# Registers a fresh service id, configures, builds, deploys good, deploys broken,
# asserts auto-rollback, checks metrics + webhook got the story.
set -u
BASE=http://localhost:7000
KIT=$HOME/acko-mohit-devops/devops-startup-kit/launchpad-starter-kit/launchpad-starter-kit
PASS=0; FAIL=0

step() { # step "label" <expected> <actual>
  if [ "$2" = "$3" ]; then PASS=$((PASS+1)); echo "[PASS] $1"; else FAIL=$((FAIL+1)); echo "[FAIL] $1 (expected $2, got $3)"; fi
}
get()  { python3 -c "import json,sys;d=json.load(sys.stdin);print(d$1)"; }

echo "== 0. preflight =="
python3 "$(dirname "$0")/../preflight.py" >/dev/null 2>&1
VER=$(python3 -c "import json;print(json.load(open('$(dirname "$0")/../preflight.json'))['status'])" 2>/dev/null || echo UNKNOWN)
step "preflight READY" "READY" "$VER"

echo "== 1. register service with team =="
RID=$(curl -s -X POST $BASE/api/services -H 'Content-Type: application/json' \
  -d "{\"name\":\"acceptance-svc\",\"repo_path\":\"$KIT/services/quote-svc\",\"port\":8080,\"team\":\"qa\"}" | get "['id']" 2>/dev/null)
if [ -z "$RID" ] || [ "$RID" = "None" ]; then
  # already registered from a previous run — look up its id
  RID=$(curl -s $BASE/api/services 2>/dev/null | python3 -c "import json,sys;d=json.load(sys.stdin);print(next(x['id'] for x in d if x['name']=='acceptance-svc'))" 2>/dev/null)
  [ -n "$RID" ] && { PASS=$((PASS+1)); echo "[PASS] service already registered, reusing id $RID"; }
fi
[ -n "$RID" ] && [ "$RID" != "None" ] && [ -n "$(curl -s -X POST $BASE/api/services -o /dev/null -w '%{http_code}' 2>/dev/null)" ] || true
if [ -z "$RID" ] || [ "$RID" = "None" ]; then FAIL=$((FAIL+1)); echo "[FAIL] register"; fi

echo "== 2. config both envs =="
C1=$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/api/services/$RID/config -H 'Content-Type: application/json' -d '{"environment":"staging","key":"DB_PASSWORD","value":"qa-stg"}')
step "staging config set" "200" "$C1"
C2=$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/api/services/$RID/config -H 'Content-Type: application/json' -d '{"environment":"prod","key":"DB_PASSWORD","value":"qa-prod"}')
step "prod config set" "200" "$C2"

echo "== 3. version discovery =="
TAGS=$(curl -s $BASE/api/services/$RID/all_versions | get "['all'][0]")
case "$TAGS" in v*) PASS=$((PASS+1)); echo "[PASS] versions listed ($TAGS newest)";; *) FAIL=$((FAIL+1)); echo "[FAIL] versions";; esac

echo "== 4. build v1.1.0 + log viewable =="
curl -s -X POST $BASE/api/services/$RID/build -H 'Content-Type: application/json' -d '{"version":"v1.1.0"}' > /dev/null
BID=$(curl -s $BASE/api/services/$RID/versions | get "['builds'][0]['id']")
LOGC=$(curl -s -o /dev/null -w '%{http_code}' $BASE/api/builds/$BID/log)
step "build log endpoint" "200" "$LOGC"
LOGB=$(curl -s $BASE/api/builds/$BID/log | python3 -c "import json,sys;print(1 if 'docker build' in json.load(sys.stdin)['log'] else 0)")
step "build log contains docker build" "1" "$LOGB"

echo "== 5. deploy good version -> healthy on staging port =="
curl -s -X POST $BASE/api/services/$RID/deploy -H 'Content-Type: application/json' -d '{"version":"v1.1.0","environment":"staging"}' > /dev/null
sleep 14
S1=$(curl -s $BASE/api/deploys | get "[0]['status']")
step "deploy v1.1.0 healthy" "healthy" "$S1"
H1=$(curl -s -o /dev/null -w '%{http_code}' -m 3 localhost:$((8100 + RID))/health)
step "health endpoint 200" "200" "$H1"

echo "== 6. deploy BROKEN v1.2.0 -> auto-rollback =="
curl -s -X POST $BASE/api/services/$RID/build -H 'Content-Type: application/json' -d '{"version":"v1.2.0"}' > /dev/null
curl -s -X POST $BASE/api/services/$RID/deploy -H 'Content-Type: application/json' -d '{"version":"v1.2.0","environment":"staging"}' > /dev/null
sleep 16
S2=$(curl -s $BASE/api/deploys | get "[0]['status']")
step "broken deploy rolled_back" "rolled_back" "$S2"
V2=$(curl -s $BASE/api/deploys | get "[0]['version']")
step "rolled back TO v1.1.0" "v1.1.0" "$V2"
H2=$(curl -s -m 3 localhost:$((8100 + RID))/version)
case "$H2" in *v1.1.0*) PASS=$((PASS+1)); echo "[PASS] running container is v1.1.0 again";; *) FAIL=$((FAIL+1)); echo "[FAIL] container version ($H2)";; esac

echo "== 7. metrics + webhook got the story =="
M=$(curl -s $BASE/api/metrics | python3 -c "import json,sys;d=json.load(sys.stdin);print(1 if any(x['service']=='acceptance-svc' and x['deploys_total']>=2 for x in d) else 0)")
step "metrics include acceptance-svc" "1" "$M"
W=$(curl -s localhost:9900/api/received | python3 -c "import json,sys;d=json.load(sys.stdin);print(1 if any('rollback' in str(w['body'].get('type','')) for w in d) else 0)")
step "webhook received rollback event" "1" "$W"

echo "== 8. manual rollback endpoint present =="
DID=$(curl -s $BASE/api/deploys | get "[0]['id']")
RB=$(curl -s -X POST $BASE/api/deploys/$DID/rollback -H 'Content-Type: application/json' -d '{"version":"v1.0.0"}')
case "$RB" in *ok*|*v1.0.0*) PASS=$((PASS+1)); echo "[PASS] manual rollback accepted";; *) FAIL=$((FAIL+1)); echo "[FAIL] manual rollback ($RB)";; esac

echo ""
echo "RESULT: $PASS passed, $FAIL failed"
[ $FAIL -eq 0 ] && echo "ALL GREEN" || echo "REVIEW NEEDED"
